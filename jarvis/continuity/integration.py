"""Legacy/v2 bridge for MAIN continuity.

Production behavior is unchanged legacy. `ContinuityIntegration` does not dispatch
to the hub, the phone listener, or the CLI, and it refuses cutover while the
deployment gates in OPERATIONAL-CONTRACT.md are open.

`IsolatedFleet` is a temp-directory loopback around the real `AuthorityStore`.
It is not a gateway deployment. Device secrets and the passkey check are test
doubles: they stop unauthenticated callers reaching the store, and they are not
installation-key, TLS, or WebAuthn proof. Receipts record `signature_verified`
false. Nothing here writes `jarvis.config.json` or `.jarvis/`.
"""

import hashlib
import hmac
import importlib
import json
import secrets
import sqlite3
from pathlib import Path

from .authority import (
    LEASE_SECONDS,
    SAFETY_MARGIN,
    AuthorityError,
    AuthorityStore,
)
from .clock import CachedAuthority, suspend_aware_clock

ORIGIN = "https://gateway.test.invalid"
FLEET_ID = "home"
AUTHORITY_ID = "oracle-auth"

PERSONAL = frozenset({
    "prompt", "body", "text", "content", "file", "filename", "path", "screenshot",
    "frame", "image", "cookie_value", "passkey", "secret", "token", "transcript",
    "workflow", "message", "input", "payload",
})

# Contract-named store codes. Extras still fail closed; they never become HTTP 200.
CONTRACT_HTTP = {
    "bad_request": 400,
    "bad_ticket": 400,
    "ticket_expired": 410,
    "stale": 409,
    "busy": 409,
    "key_reused": 409,
    "conflict": 409,
    "not_quiet": 409,
    "lease_live": 409,
    "quarantined": 503,
}
EXTRA_HTTP = {
    "writer_busy": 503,
    "authority_unavailable": 503,
    "not_found": 409,
    "ahead": 409,
    "bad_edge": 409,
    "unacked": 409,
    "expired": 409,
    "draining": 409,
    "mode": 409,
    "ineligible": 409,
    "main_alive": 409,
    "no_main": 409,
    "noop": 409,
    "not_ready": 409,
    "bad_state": 409,
    "no_admission": 409,
    "target_unfenced": 409,
    "unknown_participant": 409,
    "personal_field": 400,
    "unauthenticated": 401,
    "bad_passkey": 401,
    "not_manage_main": 403,
    "not_owner": 403,
    "not_candidate": 403,
    "signature_unverified": 403,
    "admission_stopped": 409,
    "ai_not_ready": 409,
    "v2_inactive": 409,
    "legacy_unwired": 409,
    "refuses_live_state": 403,
}
HTTP_STATUS = {**CONTRACT_HTTP, **EXTRA_HTTP}
RETRY_AFTER = frozenset({"not_quiet", "lease_live"})
STEPS = frozenset({"drain", "fence", "grant", "complete", "cancel"})
ABSENT_STEPS = frozenset({"catching_up", "verifying"})

PEER_FACADES = {
    "gateway": ("ContinuityGateway", (
        "status", "renew", "release", "acknowledge", "transition_request",
        "transition_readiness", "transition_step", "consume", "admission", "relay")),
    "ingress": ("MobileIngress", (
        "pair", "authenticate", "status", "submit", "operation",
        "manage_main_preview", "manage_main_confirm")),
    "runtime": ("MainRuntime", (
        "start", "readiness", "renew_once", "require_authority", "begin_drain",
        "release_before_suspend", "invalidate_after_resume", "stop_new_admission")),
    "executor": ("ExecutorGuard", (
        "accept", "fence", "reconcile", "stop_interactive")),
}

LEGACY_PATHS = (
    {"id": "cluster_cli.set_main", "path": "jarvis/cluster_cli.py", "v2_aware": False,
     "issues_effects": True},
    {"id": "hub.dispatch", "path": "jarvis/cluster.py", "v2_aware": False,
     "issues_effects": True},
    {"id": "phone.listener", "path": "jarvis/phone.py", "v2_aware": False,
     "issues_effects": True},
    {"id": "rest.sleep", "path": "jarvis/rest.py", "v2_aware": False,
     "issues_effects": False},
    {"id": "fleet.main_transfer", "path": "jarvis/fleet_management.py", "v2_aware": False,
     "issues_effects": False},
)

INCOMPATIBILITIES = (
    {"id": "peers_absent",
     "detail": "jarvis/continuity/{gateway,ingress,runtime,executor}.py are not in the tree."},
    {"id": "store_states",
     "detail": "AuthorityStore has no CATCHING_UP or VERIFYING states. MAIN-HANDOFF section 6 lists both."},
    {"id": "no_signatures",
     "detail": "AuthorityStore trusts its caller. No MAIN signature verifier exists, so a default executor refuses."},
    {"id": "no_passkey",
     "detail": "No WebAuthn verifier. The isolated challenge binding is not owner proof."},
    {"id": "http_extras",
     "detail": "Store codes outside the contract map fail closed (409 or 503), including not_found."},
    {"id": "phone_origin",
     "detail": "phone.py still binds 127.0.0.1:7787 on the PC that happens to be awake."},
    {"id": "cluster_main_unfenced",
     "detail": "jarvis cluster main still writes cluster.main_device and does not take a lease."},
    {"id": "hub_unfenced",
     "detail": "Hub.dispatch does not consult continuity before local or node effects."},
    {"id": "sleep_unhooked",
     "detail": "rest.sleep does not release a lease or invalidate CachedAuthority."},
    {"id": "margin_unmeasured",
     "detail": "SAFETY_MARGIN is the 2 second placeholder. It is not a measured suspend margin."},
    {"id": "windows_clock",
     "detail": "Dell has no tested suspend-aware clock and must not cache MAIN validity."},
    {"id": "no_oracle_device",
     "detail": "No verified Oracle gateway host is enrolled. Arch and Dell are not a substitute."},
    {"id": "ai_profile",
     "detail": "Luna +5 and Grok 4.7 are routing intent only. A provider name is not ai_ready."},
    {"id": "inprocess_auth_double",
     "detail": "IsolatedFleet compares shared secrets in-process. That is not mutual TLS or an installation key."},
)

_CONSUME = ("admission_id", "target", "operation_id", "attempt_id", "digest", "main",
            "incarnation", "epoch", "issued_at", "admit_until", "issuer_incarnation")


class IntegrationError(Exception):
    """Refusal that carries a stable code and an HTTP status. The message is the code only."""

    def __init__(self, code, wait=None):
        super().__init__(code)
        self.code = code
        self.wait = wait
        status, retry = http_status(code, wait)
        self.http = status
        self.retry_after = retry


def http_status(code, wait=None):
    """Map a refusal to HTTP. Unknown codes are 503. None of them are success."""
    status = HTTP_STATUS.get(code, 503)
    retry = wait if code in RETRY_AFTER and isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait > 0 else None
    return status, retry


def reject_personal(payload):
    if not isinstance(payload, dict):
        raise IntegrationError("bad_request")
    if PERSONAL.intersection(payload):
        raise IntegrationError("personal_field")
    return payload


def operation_digest(target, operation_id, attempt_id, effect_class):
    raw = json.dumps([target, operation_id, attempt_id, effect_class], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def peer_report():
    """Import peer modules if they exist. Do not construct them."""
    report = {}
    for name, (class_name, methods) in PEER_FACADES.items():
        module_name = "jarvis.continuity." + name
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            report[name] = {"present": False, "compatible": False, "missing_class": class_name,
                            "missing": list(methods)}
            continue
        cls = getattr(module, class_name, None)
        if cls is None:
            report[name] = {"present": True, "compatible": False, "missing_class": class_name,
                            "missing": list(methods)}
            continue
        missing = [method for method in methods if not callable(getattr(cls, method, None))]
        report[name] = {"present": True, "compatible": not missing, "missing_class": None,
                        "missing": missing}
    return report


def deployment_facts(config_path=None):
    """Read-only cluster identity. Device records, keys, and addresses are not copied."""
    path = Path(config_path) if config_path else Path(__file__).resolve().parents[2] / "jarvis.config.json"
    facts = {
        "config_read": False,
        "device_ids": [],
        "oracle_device": False,
        "main_device": None,
        "tunnel": None,
        "listen_host": None,
        "listen_port": None,
        "phone_bind": "127.0.0.1:7787",
        "linux_boottime": suspend_aware_clock() is not None,
        "windows_clock_tested": False,
        "provider_verified": False,
    }
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return facts
    if not isinstance(document, dict):
        return facts
    facts["config_read"] = True
    devices = document.get("devices") if isinstance(document.get("devices"), dict) else {}
    facts["device_ids"] = sorted(key for key, value in devices.items() if isinstance(value, dict))
    facts["oracle_device"] = "oracle" in facts["device_ids"]
    cluster = document.get("cluster") if isinstance(document.get("cluster"), dict) else {}
    for key in ("main_device", "tunnel", "listen_host"):
        value = cluster.get(key)
        facts[key] = value if isinstance(value, str) else None
    port = cluster.get("listen_port")
    facts["listen_port"] = port if isinstance(port, int) and not isinstance(port, bool) else None
    return facts


def _blockers(facts=None, peers=None):
    facts = facts if facts is not None else deployment_facts()
    peers = peers if peers is not None else peer_report()
    rows = []
    for name, peer in peers.items():
        if not peer["compatible"]:
            rows.append({"id": "peer_" + name, "why": "missing or incompatible " + name})
    if not facts.get("oracle_device"):
        rows.append({"id": "gateway_host_unverified",
                     "why": "No enrolled Oracle host. This config cannot prove a stable gateway."})
    if facts.get("listen_host") in (None, "127.0.0.1", "localhost"):
        rows.append({"id": "hub_loopback", "why": "The live hub listens on loopback, not a stable gateway origin."})
    rows.extend((
        {"id": "phone_origin_arch_loopback", "why": "The phone listener is 127.0.0.1:7787 on one PC."},
        {"id": "dell_provider_unverified", "why": "Dell Luna/Grok session readiness was not verified."},
        {"id": "luna_grok_unverified", "why": "Provider names are not ai_ready and no paid API may be used."},
        {"id": "safety_margin_unmeasured", "why": "SAFETY_MARGIN is an unmeasured 2 second placeholder."},
        {"id": "windows_clock_untested", "why": "Dell must validate online; it has no tested suspend-aware cache."},
        {"id": "legacy_paths_unfenced", "why": "CLI, hub, and phone can still act without a v2 lease."},
        {"id": "passkey_verifier_absent", "why": "No fresh WebAuthn manage_main verifier is wired."},
        {"id": "signature_verifier_absent", "why": "No MAIN operation signature verifier is wired."},
        {"id": "automatic_failover_disabled", "why": "Recovery stays manual. This object does not auto-promote."},
    ))
    return rows


def _blocked(action):
    return {
        "action": action,
        "applied": False,
        "activated": False,
        "mode": "LEGACY_ACTIVE",
        "v2_dispatch": False,
        "legacy_entry_points_unchanged": True,
        "production_failover": False,
        "ignored_tuple": True,
        "blockers": _blockers(),
    }


class ContinuityIntegration:
    """Live bridge. Cutover methods refuse and do not open an authority database."""

    def mode(self):
        return {
            "mode": "LEGACY_ACTIVE",
            "source": "default_until_explicit_cutover",
            "v2_dispatch": False,
            "legacy_entry_points_unchanged": True,
            "production_failover": False,
            "blockers": self.blockers(),
        }

    def blockers(self):
        return _blockers()

    def legacy_request(self, request):
        reject_personal(request if isinstance(request, dict) else {})
        return {"acted": False, "ok": False, "code": "legacy_unwired", "http": 409,
                "mode": "LEGACY_ACTIVE",
                "note": "Live CLI, hub, and phone entry points are unchanged. This method does not call them."}

    def v2_request(self, principal, operation_id, request):
        reject_personal(request if isinstance(request, dict) else {})
        return {"ok": False, "acted": False, "code": "v2_inactive", "http": 409,
                "operation_id": operation_id, "retry": False, "production_failover": False}

    def participants(self):
        facts = deployment_facts()
        return {"device_ids": list(facts["device_ids"]), "oracle_enrolled": bool(facts["oracle_device"]),
                "eligible_acked": False, "legacy_paths": [dict(row) for row in LEGACY_PATHS]}

    def prepare_v2(self, expected_mode_version):
        return _blocked("prepare_v2")

    def quiesce_legacy(self, expected_mode_version):
        return _blocked("quiesce_legacy")

    def activate_v2(self, authority_tuple, canary):
        return _blocked("activate_v2")

    def rollback_quiesced(self, reason, expected_mode_version):
        return _blocked("rollback_quiesced")


def _match_secret(offered, expected):
    if not isinstance(offered, str) or not isinstance(expected, str) or len(offered) != len(expected):
        return False
    return hmac.compare_digest(offered, expected)


class LoopbackGateway:
    """In-process store client. Not ContinuityGateway and not an HTTPS server."""

    def __init__(self, store):
        self.store = store
        self.up = True
        self.log = []
        self.enrollment = {}
        self.proofs = {}
        self.renew_calls = 0

    def enroll(self, device, installation_id, secret):
        self.enrollment[device] = {"installation_id": installation_id, "secret": secret}

    def mint_owner_proof(self):
        proof = "own-" + secrets.token_hex(8)
        self.proofs[proof] = {"used": False, "result": None, "bound": None}
        return proof

    def audit(self, **fields):
        if PERSONAL.intersection(fields):
            raise IntegrationError("personal_field")
        self.log.append(fields)

    def _down(self):
        if not self.up:
            raise IntegrationError("authority_unavailable")

    def _call(self, fn):
        self._down()
        try:
            return fn()
        except AuthorityError as exc:
            self.audit(code=exc.code)
            raise IntegrationError(exc.code, exc.wait) from None

    def _pc(self, identity):
        if not isinstance(identity, dict):
            raise IntegrationError("unauthenticated")
        reject_personal(identity)
        device = identity.get("device")
        row = self.enrollment.get(device)
        if row is None or identity.get("installation_id") != row["installation_id"]:
            raise IntegrationError("unauthenticated")
        if not _match_secret(identity.get("secret"), row["secret"]):
            raise IntegrationError("unauthenticated")
        return device

    def _owner(self, identity):
        if self._pc(identity) != "owner-pc":
            raise IntegrationError("not_owner")
        return "owner-pc"

    def status(self, identity):
        self._pc(identity)
        return self._call(self.store.status)

    def renew(self, identity, lease):
        reject_personal(lease)
        device = self._pc(identity)
        if device != lease.get("holder"):
            raise IntegrationError("unauthenticated")
        self._down()
        self.renew_calls += 1

        def run():
            return self.store.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])

        view = self._call(run)
        self.audit(op="renew", device=device, ready=bool(view.get("ready")))
        return view

    def release(self, identity, lease):
        reject_personal(lease)
        device = self._pc(identity)
        if device != lease.get("holder"):
            raise IntegrationError("unauthenticated")

        def run():
            self.store.release(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
            return self.store.status()

        view = self._call(run)
        self.audit(op="release", device=device, mode=view["mode"])
        return view

    def acknowledge(self, identity, mode_version, fenced_epoch):
        device = self._pc(identity)
        installation = identity["installation_id"]
        self._call(lambda: self.store.acknowledge(device, installation, mode_version, fenced_epoch))
        self.audit(op="ack", device=device, mode_version=mode_version, fenced_epoch=fenced_epoch)
        return {"device": device, "mode_version": mode_version, "fenced_epoch": fenced_epoch}

    def _take_proof(self, proof, bound):
        row = self.proofs.get(proof)
        if row is None:
            raise IntegrationError("unauthenticated")
        if row["used"]:
            if row["bound"] == bound and row["result"] is not None:
                return row["result"]
            raise IntegrationError("unauthenticated")
        row["used"] = True
        row["bound"] = bound
        return None

    def transition_request(self, proof, request):
        reject_personal(request)
        if request.get("action") not in ("make_main_now", "set_preferred_main", "recover"):
            raise IntegrationError("bad_request")
        if request.get("profile") not in ("control", "ai"):
            raise IntegrationError("bad_request")
        bound = (request["action"], request["candidate"], request["profile"], request["client_key"])
        prior = self._take_proof(proof, bound)
        if prior is not None:
            return prior
        view = self._call(lambda: self.store.request(
            "owner-pc", request["client_key"], request["action"], request["candidate"],
            request["expected_incarnation"], request["expected_epoch"], request["expected_policy"],
            request["profile"]))
        self.proofs[proof]["result"] = view
        self.audit(op="transition", transition_id=view["transition_id"], action=view["action"])
        return view

    def transition_readiness(self, identity, transition_id, readiness):
        reject_personal(readiness)
        device = self._pc(identity)
        current = self._call(lambda: self.store.transition(transition_id))
        if current["candidate"] != device:
            raise IntegrationError("not_candidate")
        checkpoint = readiness.get("checkpoint")
        view = self._call(lambda: self.store.readiness(
            transition_id, bool(readiness.get("control_ready")), bool(readiness.get("ai_ready")), checkpoint))
        self.audit(op="readiness", transition_id=transition_id, device=device,
                   control=bool(view["control_ready"]), ai=bool(view["ai_ready"]))
        return view

    def transition_step(self, identity, transition_id, step, canary=None):
        if step in ABSENT_STEPS or step not in STEPS:
            raise IntegrationError("bad_request")
        if step == "grant":
            device = self._pc(identity)
            current = self._call(lambda: self.store.transition(transition_id))
            if current["candidate"] != device:
                raise IntegrationError("not_candidate")
            lease = self._call(lambda: self.store.grant(transition_id))
            self.audit(op="grant", holder=lease["holder"], epoch=lease["epoch"], ready=False)
            return lease
        self._owner(identity)
        if step == "drain":
            view = self._call(lambda: self.store.drain(transition_id))
        elif step == "fence":
            view = self._call(lambda: self.store.fence(transition_id))
        elif step == "cancel":
            view = self._call(lambda: self.store.cancel(transition_id))
        else:
            if canary is not True and canary is not False:
                raise IntegrationError("bad_request")
            view = self._call(lambda: self.store.complete(transition_id, canary))
        self.audit(op=step, transition_id=transition_id, state=view["state"])
        return view

    def consume(self, identity, ticket):
        reject_personal(ticket)
        device = self._pc(identity)
        if device != ticket.get("main"):
            raise IntegrationError("unauthenticated")
        fields = {key: ticket[key] for key in _CONSUME}
        receipt = self._call(lambda: self.store.consume(**fields))
        self.audit(op="consume", admission_id=ticket["admission_id"], already=bool(receipt["already"]),
                   target=ticket["target"], epoch=ticket["epoch"])
        return receipt

    def admission(self, identity, admission_id):
        self._pc(identity)
        return self._call(lambda: self.store.admission(admission_id))

    def relay(self, principal, operation_id, bounded):
        reject_personal(bounded if isinstance(bounded, dict) else {})
        if not isinstance(principal, dict) or principal.get("role") != "phone":
            raise IntegrationError("unauthenticated")
        if principal.get("authenticated_by") != "session" or not principal.get("session_id"):
            raise IntegrationError("unauthenticated")
        view = self._call(self.store.status)
        self.audit(op="relay", operation_id=operation_id, main=view["main"], ready=view["main_ready"])
        if not view["main_ready"]:
            raise IntegrationError("no_admission")
        return {"ok": True, "main": view["main"], "incarnation": view["incarnation"], "epoch": view["epoch"],
                "operation_id": operation_id, "digest": bounded["digest"], "target": bounded["target"],
                "effect_class": bounded["effect_class"]}


class LocalExecutor:
    """Durable target journal. Default construction refuses every effect: no signature verifier."""

    def __init__(self, path, gateway, device, *, require_signature=True):
        self.path = Path(path)
        self.gateway = gateway
        self.device = device
        self.require_signature = require_signature
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS seen(
              device_id TEXT PRIMARY KEY, incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(
              target TEXT NOT NULL, operation_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
              admission_id TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
              run_count INTEGER NOT NULL, incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL,
              signature_verified INTEGER NOT NULL, PRIMARY KEY(target, operation_id, attempt_id));
        """)

    def close(self):
        self._db.close()

    def reopen(self):
        """Process restart: same file, empty memory, including a lost in-flight effect."""
        self.close()
        revived = LocalExecutor(self.path, self.gateway, self.device, require_signature=self.require_signature)
        return revived

    def _seen(self):
        return self._db.execute("SELECT * FROM seen WHERE device_id=?", (self.device,)).fetchone()

    def _mark_seen(self, incarnation, epoch):
        row = self._seen()
        if row is None or (incarnation, epoch) > (row["incarnation"], row["epoch"]):
            self._db.execute("INSERT INTO seen(device_id, incarnation, epoch) VALUES(?,?,?)"
                             " ON CONFLICT(device_id) DO UPDATE SET incarnation=excluded.incarnation,"
                             " epoch=excluded.epoch", (self.device, incarnation, epoch))

    def _receipt(self, operation_id, attempt_id):
        return self._db.execute(
            "SELECT * FROM receipts WHERE target=? AND operation_id=? AND attempt_id=?",
            (self.device, operation_id, attempt_id)).fetchone()

    def reconcile(self, operation_id, attempt_id):
        row = self._receipt(operation_id, attempt_id)
        if row is None:
            return {"state": "unknown", "operation_id": operation_id, "attempt_id": attempt_id, "run_count": 0}
        return _receipt_view(row)

    def fence(self, incarnation, epoch, mode_version):
        """Remember an epoch the store has already moved to. Older epochs fail closed offline."""
        del mode_version
        self._mark_seen(int(incarnation), int(epoch))
        row = self._seen()
        return {"device": self.device, "incarnation": row["incarnation"], "epoch": row["epoch"]}

    def stop_interactive(self, session_id, reason):
        self._db.execute(
            "INSERT OR REPLACE INTO receipts(target, operation_id, attempt_id, admission_id, digest, state,"
            " run_count, incarnation, epoch, signature_verified) VALUES(?,?,?,?,?,?,?,?,?,0)",
            (self.device, "interactive-" + session_id, "stop", "none", operation_digest(
                self.device, session_id, "stop", "interactive"), "stopped", 0, 0, 0))
        return {"session_id": session_id, "state": "stopped", "reason_code": reason, "replayed": False}

    def accept(self, identity, operation, ticket):
        reject_personal(operation if isinstance(operation, dict) else {})
        reject_personal(ticket)
        if self.require_signature:
            raise IntegrationError("signature_unverified")
        if ticket.get("target") != self.device:
            raise IntegrationError("bad_request")
        prior = self._receipt(ticket["operation_id"], ticket["attempt_id"])
        if prior is not None:
            if prior["digest"] != ticket["digest"] or prior["admission_id"] != ticket["admission_id"]:
                raise IntegrationError("conflict")
            return _receipt_view(prior)
        seen = self._seen()
        if seen is not None and (ticket["incarnation"], ticket["epoch"]) < (seen["incarnation"], seen["epoch"]):
            raise IntegrationError("stale")
        consumed = self.gateway.consume(identity, ticket)
        if consumed["already"]:
            self._insert(ticket, "reconciled", 0)
            self._mark_seen(ticket["incarnation"], ticket["epoch"])
            return self.reconcile(ticket["operation_id"], ticket["attempt_id"])
        self._insert(ticket, "starting", 0)
        if operation.get("crash") is True:
            self._update(ticket, "uncertain", 0)
            return self.reconcile(ticket["operation_id"], ticket["attempt_id"])
        self._update(ticket, "accepted", 1)
        self._mark_seen(ticket["incarnation"], ticket["epoch"])
        return self.reconcile(ticket["operation_id"], ticket["attempt_id"])

    def _insert(self, ticket, state, run_count):
        self._db.execute(
            "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,0)",
            (self.device, ticket["operation_id"], ticket["attempt_id"], ticket["admission_id"],
             ticket["digest"], state, run_count, ticket["incarnation"], ticket["epoch"]))

    def _update(self, ticket, state, run_count):
        self._db.execute(
            "UPDATE receipts SET state=?, run_count=? WHERE target=? AND operation_id=? AND attempt_id=?",
            (state, run_count, self.device, ticket["operation_id"], ticket["attempt_id"]))

    def runs(self, operation_id):
        row = self._db.execute(
            "SELECT COALESCE(SUM(run_count), 0) AS n FROM receipts WHERE target=? AND operation_id=?",
            (self.device, operation_id)).fetchone()
        return int(row["n"])


def _receipt_view(row):
    return {"target": row["target"], "operation_id": row["operation_id"], "attempt_id": row["attempt_id"],
            "admission_id": row["admission_id"], "digest": row["digest"], "state": row["state"],
            "run_count": row["run_count"], "incarnation": row["incarnation"], "epoch": row["epoch"],
            "signature_verified": False, "replayed": row["state"] != "starting"}


class PcRuntime:
    """One PC's lease cache. Windows-shaped runtimes pass clock=None and never cache."""

    def __init__(self, device, gateway, identity, *, clock, boot_id):
        self.device = device
        self.gateway = gateway
        self.identity = identity
        self.online_only = clock is None
        self.cache = CachedAuthority(clock=clock, boot=lambda: boot_id)
        self.boot_id = boot_id
        self.lease = None
        self.admitting = False
        self.ai_ready = False
        self.asleep = False

    def start(self):
        return {"device": self.device, "admitting": self.admitting, "ai_ready": False,
                "online_only": self.online_only}

    def readiness(self, profile="control"):
        if profile not in ("control", "ai"):
            raise IntegrationError("bad_request")
        return {"device": self.device, "profile": profile, "control_ready": True, "ai_ready": False,
                "reason": "provider_unverified" if profile == "ai" else "control_only",
                "provider": None}

    def hold(self, lease, *, ready):
        self.lease = dict(lease)
        self.admitting = bool(ready)
        self.ai_ready = False
        started = self.cache.now()
        if started is not None:
            self.cache.observe(holder=self.device, incarnation=lease["incarnation"], epoch=lease["epoch"],
                               valid_for=lease["valid_for"], ready=ready, request_started=started)

    def renew_once(self):
        if self.lease is None:
            raise IntegrationError("no_main")
        started = self.cache.now()
        view = self.gateway.renew(self.identity, self.lease)
        self.cache.observe(holder=self.device, incarnation=self.lease["incarnation"], epoch=self.lease["epoch"],
                           valid_for=view["valid_for"], ready=view["ready"], request_started=started)
        self.admitting = bool(view["ready"]) and not self.asleep
        return view

    def require_authority(self, effect_class, target=None):
        del target
        if not self.admitting:
            return {"ok": False, "code": "admission_stopped", "acted": False, "source": "local"}
        if effect_class == "ai":
            return {"ok": False, "code": "ai_not_ready", "acted": False, "ai_ready": False}
        if not self.online_only and self.cache.remaining(self.device, require_ready=True) > 0:
            return {"ok": True, "source": "cache", "acted": False,
                    "cached_seconds": self.cache.remaining(self.device)}
        try:
            view = self.renew_once()
        except IntegrationError as exc:
            return {"ok": False, "code": exc.code, "http": exc.http, "acted": False, "source": "online"}
        if not view.get("ready"):
            return {"ok": False, "code": "not_ready", "acted": False, "source": "online"}
        return {"ok": True, "source": "online", "acted": False, "cached_seconds": 0.0}

    def begin_drain(self, transition_id):
        self.stop_new_admission("drain")
        return {"transition_id": transition_id, "admitting": False}

    def release_before_suspend(self):
        self.stop_new_admission("suspend")
        released = False
        code = None
        if self.lease is not None:
            try:
                self.gateway.release(self.identity, self.lease)
                released = True
            except IntegrationError as exc:
                code = exc.code
        self.invalidate_after_resume()
        self.cache.invalidate("suspend")
        return {"released": released, "code": code, "admitting": False}

    def invalidate_after_resume(self):
        self.cache.invalidate("resumed")
        self.lease = None
        self.admitting = False
        self.asleep = False

    def stop_new_admission(self, reason):
        self.admitting = False
        self.asleep = reason == "asleep"
        return {"admitting": False, "reason": reason}

    def fall_asleep(self):
        """Process stops renewing and admitting. It does not ack a fence and may not reach release."""
        self.stop_new_admission("asleep")
        return {"released": False, "admitting": False}

    def wake(self):
        self.asleep = False
        self.cache.invalidate("resumed")
        self.admitting = False
        self.lease = None


class MobileIngress:
    """Session table for the isolated loopback. Metadata only. Not the live phone server."""

    def __init__(self, gateway, runtimes, executors, identities, clock):
        self.gateway = gateway
        self.runtimes = runtimes
        self.executors = executors
        self.identities = identities
        self.clock = clock
        self.sessions = {}
        self.ops = {}
        self.previews = {}
        self.pairing_code = "pair-" + secrets.token_hex(4)
        self.pairing_used = False

    def pair(self, pairing_proof, client_metadata):
        reject_personal(client_metadata or {})
        if self.pairing_used or pairing_proof != self.pairing_code:
            raise IntegrationError("unauthenticated")
        self.pairing_used = True
        cookie = "ck-" + secrets.token_hex(8)
        session = {"id": "ses-" + secrets.token_hex(8), "role": "phone", "manage_main": False,
                   "csrf": "csrf-" + secrets.token_hex(8), "origin": ORIGIN, "input_open": True}
        self.sessions[cookie] = session
        return {"session_id": session["id"], "cookie": cookie, "csrf": session["csrf"],
                "manage_main": False, "origin": ORIGIN}

    def authenticate(self, cookie, csrf_token, request_origin):
        session = self.sessions.get(cookie)
        if session is None or csrf_token != session["csrf"] or request_origin != session["origin"]:
            raise IntegrationError("unauthenticated")
        return {"role": "phone", "session_id": session["id"], "manage_main": False,
                "authenticated_by": "session", "cookie": cookie}

    def status(self, principal):
        self._session(principal)
        try:
            view = self.gateway.status(self.identities["owner-pc"])
        except IntegrationError as exc:
            return {"ok": False, "code": exc.code, "http": exc.http, "stale": True, "commands": False}
        return {"ok": True, "mode": view["mode"], "main": view["main"], "main_ready": view["main_ready"],
                "epoch": view["epoch"], "commands": False, "manage_main": False}

    def submit(self, principal, operation_id, request):
        session = self._session(principal)
        reject_personal(request)
        if operation_id in self.ops:
            return dict(self.ops[operation_id])
        record = {"operation_id": operation_id, "state": "held", "code": None, "run_count": 0,
                  "session_id": session["id"], "cursor": 0}
        self.ops[operation_id] = record
        try:
            bounded = self._bounded(operation_id, request)
            relay = self.gateway.relay(principal, operation_id, bounded)
            runtime = self.runtimes[relay["main"]]
            decision = runtime.require_authority(relay["effect_class"], relay["target"])
            if not decision["ok"]:
                record.update(state="rejected", code=decision["code"], cursor=1)
                return dict(record)
            ticket = self._ticket(bounded, relay)
            receipt = self.executors[relay["target"]].accept(self.identities[relay["main"]], {}, ticket)
            record.update(state=receipt["state"], code=None, run_count=receipt["run_count"],
                          admission_id=receipt["admission_id"], epoch=receipt["epoch"], cursor=2)
        except IntegrationError as exc:
            record.update(state="rejected", code=exc.code, cursor=1)
        return dict(record)

    def operation(self, principal, operation_id, cursor=None):
        self._session(principal)
        row = self.ops.get(operation_id)
        if row is None:
            return {"operation_id": operation_id, "state": "unknown", "cursor": cursor, "run_count": 0}
        return dict(row)

    def hold(self, principal, operation_id, request):
        """Remember an explicit operation id and do not execute it."""
        session = self._session(principal)
        reject_personal(request)
        if operation_id in self.ops:
            return dict(self.ops[operation_id])
        record = {"operation_id": operation_id, "state": "held", "code": None, "run_count": 0,
                  "session_id": session["id"], "cursor": 0, "target": request["target"],
                  "effect_class": request["effect_class"]}
        self.ops[operation_id] = record
        return dict(record)

    def note_input(self, principal, token):
        session = self._session(principal)
        if not session["input_open"]:
            return {"accepted": False, "replayed": False}
        if not isinstance(token, str) or token in PERSONAL:
            raise IntegrationError("personal_field")
        session.setdefault("dropped", 0)
        session["dropped"] = 0
        return {"accepted": True, "persisted": False}

    def disconnect(self, cookie):
        session = self.sessions.get(cookie)
        if session is None:
            raise IntegrationError("unauthenticated")
        session["input_open"] = False
        stopped = self.executors["dell"].stop_interactive(session["id"], "disconnect")
        return {"session_id": session["id"], "input_open": False, "stopped": stopped["state"]}

    def manage_main_preview(self, principal, request):
        if not principal.get("manage_main"):
            raise IntegrationError("not_manage_main")
        reject_personal(request)
        view = self.gateway.status(self.identities["owner-pc"])
        if view["transition"]:
            raise IntegrationError("busy")
        preview_id = "pv-" + secrets.token_hex(4)
        proof = self.gateway.mint_owner_proof()
        challenge = "ch-" + secrets.token_hex(4)
        self.previews[preview_id] = {"proof": proof, "challenge": challenge, "request": dict(request),
                                     "incarnation": view["incarnation"], "epoch": view["epoch"],
                                     "policy": view["policy_version"]}
        return {"preview_id": preview_id, "challenge": challenge, "mutates": False,
                "incarnation": view["incarnation"], "epoch": view["epoch"], "policy": view["policy_version"],
                "passkey_verifier": False}

    def manage_main_confirm(self, principal, preview_id, passkey_proof):
        if not principal.get("manage_main"):
            raise IntegrationError("not_manage_main")
        preview = self.previews.get(preview_id)
        if preview is None or passkey_proof != preview["challenge"]:
            raise IntegrationError("bad_passkey")
        view = self.gateway.status(self.identities["owner-pc"])
        if (view["incarnation"], view["epoch"], view["policy_version"]) != (
                preview["incarnation"], preview["epoch"], preview["policy"]):
            raise IntegrationError("stale")
        return self.gateway.transition_request(preview["proof"], preview["request"])

    def _session(self, principal):
        if not isinstance(principal, dict) or principal.get("authenticated_by") != "session":
            raise IntegrationError("unauthenticated")
        for session in self.sessions.values():
            if session["id"] == principal.get("session_id"):
                return session
        raise IntegrationError("unauthenticated")

    def _bounded(self, operation_id, request):
        effect = request.get("effect_class")
        target = request.get("target")
        if effect not in ("control", "ai") or not isinstance(target, str):
            raise IntegrationError("bad_request")
        attempt = request.get("attempt_id") or "a-1"
        return {"operation_id": operation_id, "attempt_id": attempt, "target": target,
                "effect_class": effect,
                "digest": operation_digest(target, operation_id, attempt, effect)}

    def _ticket(self, bounded, relay):
        wall = self.clock.wall()
        return {"admission_id": "adm-" + bounded["operation_id"], "target": bounded["target"],
                "operation_id": bounded["operation_id"], "attempt_id": bounded["attempt_id"],
                "digest": bounded["digest"], "main": relay["main"], "incarnation": relay["incarnation"],
                "epoch": relay["epoch"], "issued_at": wall - 1, "admit_until": wall + 3600,
                "issuer_incarnation": relay["incarnation"], "effect_class": bounded["effect_class"]}


class IsolatedFleet:
    """Temp-store Arch/Dell/phone loopback. Production integration stays legacy."""

    def __init__(self, directory, clock):
        root = Path(directory).resolve()
        live = Path(__file__).resolve().parents[2] / ".jarvis"
        if root == live or live in root.parents:
            raise IntegrationError("refuses_live_state")
        self.root = root
        self.clock = clock
        self.db_path = root / "authority.sqlite"
        self.store = self._open_store()
        self.gateway = LoopbackGateway(self.store)
        self.secrets = {device: secrets.token_hex(16) for device in
                        ("zenbook", "dell", "oracle", "owner-pc")}
        for device in self.secrets:
            installation = "owner-inst" if device == "owner-pc" else device + "-inst"
            self.gateway.enroll(device, installation, self.secrets[device])
        self.identities = {device: self._identity(device) for device in self.secrets}
        boot_clock = clock.mono
        self.arch = PcRuntime("zenbook", self.gateway, self.identities["zenbook"],
                              clock=boot_clock, boot_id="arch-boot-1")
        self.dell = PcRuntime("dell", self.gateway, self.identities["dell"], clock=None, boot_id="dell-boot-1")
        self.arch_exec = LocalExecutor(root / "zenbook-receipts.sqlite", self.gateway, "zenbook",
                                       require_signature=False)
        self.dell_exec = LocalExecutor(root / "dell-receipts.sqlite", self.gateway, "dell",
                                       require_signature=False)
        self.ingress = MobileIngress(
            self.gateway, {"zenbook": self.arch, "dell": self.dell},
            {"zenbook": self.arch_exec, "dell": self.dell_exec}, self.identities, clock)
        self.production = ContinuityIntegration()
        self.signature_verified = False
        self.production_failover = False

    def _open_store(self):
        return AuthorityStore(self.db_path, FLEET_ID, AUTHORITY_ID, monotonic=self.clock.mono,
                              wall=self.clock.wall, lease_seconds=LEASE_SECONDS, margin=SAFETY_MARGIN)

    def _identity(self, device):
        installation = "owner-inst" if device == "owner-pc" else device + "-inst"
        return {"device": device, "installation_id": installation, "secret": self.secrets[device]}

    def close(self):
        for executor in (getattr(self, "arch_exec", None), getattr(self, "dell_exec", None)):
            if executor is not None:
                executor.close()
        if getattr(self, "store", None) is not None:
            self.store.close()

    def dispatch_flags(self):
        view = self.store.status()
        legacy = view["mode"] == "LEGACY_ACTIVE"
        v2 = view["mode"] == "V2_ACTIVE" and bool(view["main_ready"])
        return {"mode": view["mode"], "legacy": legacy, "v2": v2, "both": bool(legacy and v2),
                "production_mode": self.production.mode()["mode"]}

    def cutover(self):
        for device, kind in (("zenbook", "pc"), ("dell", "pc"), ("oracle", "server")):
            self.store.register_participant(device, device + "-inst", kind)
        version = self.store.status()["mode_version"]
        version = self.store.set_mode("PREPARING_V2", version)
        version = self.store.set_mode("QUIESCED", version)
        for device in ("zenbook", "dell", "oracle"):
            self.gateway.acknowledge(self.identities[device], version + 1, 0)
        self.store.set_mode("V2_ONLY_NO_MAIN", version)
        return self.store.status()

    def _request(self, candidate, key, profile):
        view = self.store.status()
        action = "recover" if view["main"] is None else "make_main_now"
        proof = self.gateway.mint_owner_proof()
        return self.gateway.transition_request(proof, {
            "action": action, "candidate": candidate, "profile": profile, "client_key": key,
            "expected_incarnation": view["incarnation"], "expected_epoch": view["epoch"],
            "expected_policy": view["policy_version"]})

    def try_request(self, candidate, key, profile="control"):
        try:
            return {"ok": True, "view": self._request(candidate, key, profile)}
        except IntegrationError as exc:
            return {"ok": False, "code": exc.code, "http": exc.http, "retry_after": exc.retry_after}

    def prepare_candidate(self, transition_id, candidate, *, control=True, ai=False, profile="control"):
        del profile
        self.gateway.transition_readiness(self.identities[candidate], transition_id, {
            "control_ready": control, "ai_ready": ai, "checkpoint": "cp-1"})
        return self.gateway.transition_step(self.identities["owner-pc"], transition_id, "drain")

    def try_fence(self, transition_id):
        try:
            view = self.gateway.transition_step(self.identities["owner-pc"], transition_id, "fence")
            return {"ok": True, "view": view}
        except IntegrationError as exc:
            return {"ok": False, "code": exc.code, "http": exc.http, "retry_after": exc.retry_after}

    def ack_awake(self):
        view = self.store.status()
        for device, runtime in (("zenbook", self.arch), ("dell", self.dell), ("oracle", None)):
            if runtime is not None and runtime.asleep:
                continue
            self.gateway.acknowledge(self.identities[device], view["mode_version"], view["epoch"])

    def grant_candidate(self, transition_id, candidate):
        return self.gateway.transition_step(self.identities[candidate], transition_id, "grant")

    def finish(self, transition_id, candidate, canary):
        view = self.gateway.transition_step(self.identities["owner-pc"], transition_id, "complete", canary=canary)
        return view

    def boot_zenbook(self):
        self.cutover()
        opened = self._request("zenbook", "boot-zenbook", "control")
        self.prepare_candidate(opened["transition_id"], "zenbook", control=True, ai=False)
        fenced = self.try_fence(opened["transition_id"])
        if not fenced["ok"]:
            raise IntegrationError(fenced["code"], fenced["retry_after"])
        lease = self.grant_candidate(opened["transition_id"], "zenbook")
        self.arch.hold(lease, ready=False)
        refused = self._direct_accept("zenbook", "op-too-soon", "zenbook", lease)
        self.finish(opened["transition_id"], "zenbook", True)
        self.arch.renew_once()
        return {"lease": self.arch.lease, "not_ready": refused, "status": self.store.status()}

    def sleep_promote_dell(self, during_sleep=None):
        """Arch stops renewing. Dell is not MAIN until the old validity and margin drain."""
        old = {key: self.arch.lease[key] for key in
               ("holder", "lease_id", "incarnation", "epoch", "valid_for", "ready")}
        cache_while_asleep = None
        self.arch.fall_asleep()
        early_local = self.arch.require_authority("control")
        if self.arch.cache.now() is not None:
            cache_while_asleep = self.arch.cache.remaining("zenbook")
        during = during_sleep() if during_sleep else None
        early = self.try_request("dell", "recover-dell")
        self.clock.tick(LEASE_SECONDS - 5)
        still = self.try_request("dell", "recover-still")
        self.clock.tick(6)
        opened = self._request("dell", "recover-dell-2", "control")
        self.prepare_candidate(opened["transition_id"], "dell", control=True, ai=False)
        self.ack_awake()
        waited = self.try_fence(opened["transition_id"])
        if waited["ok"] or not waited["retry_after"]:
            raise IntegrationError("bad_state")
        self.clock.tick(waited["retry_after"] + 0.01)
        fenced = self.try_fence(opened["transition_id"])
        if not fenced["ok"]:
            raise IntegrationError(fenced["code"], fenced["retry_after"])
        lease = self.grant_candidate(opened["transition_id"], "dell")
        self.dell.hold(lease, ready=False)
        not_ready = self._direct_accept("dell", "op-before-canary", "dell", lease)
        self.finish(opened["transition_id"], "dell", True)
        self.dell.renew_once()
        self.dell_exec.fence(lease["incarnation"], lease["epoch"], self.store.status()["mode_version"])
        self.arch.wake()
        return {"old_lease": old, "early_local": early_local, "cache_while_asleep": cache_while_asleep,
                "during_sleep": during, "early": early, "still_alive": still, "waited": waited,
                "not_ready": not_ready, "status": self.store.status(),
                "dell_cached_seconds": self.dell.cache.remaining("dell")}

    def _direct_accept(self, main, operation_id, target, lease):
        ticket = self.ticket(main, operation_id, target, lease)
        try:
            receipt = self.executors()[target].accept(self.identities[main], {}, ticket)
            return {"ok": True, "receipt": receipt}
        except IntegrationError as exc:
            return {"ok": False, "code": exc.code, "http": exc.http}

    def executors(self):
        return {"zenbook": self.arch_exec, "dell": self.dell_exec}

    def ticket(self, main, operation_id, target, lease, attempt="a-1"):
        wall = self.clock.wall()
        return {"admission_id": "adm-" + operation_id, "target": target, "operation_id": operation_id,
                "attempt_id": attempt, "digest": operation_digest(target, operation_id, attempt, "control"),
                "main": main, "incarnation": lease["incarnation"], "epoch": lease["epoch"],
                "issued_at": wall - 1, "admit_until": wall + 3600, "issuer_incarnation": lease["incarnation"]}

    def persisted_bytes(self):
        blob = bytearray()
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                blob.extend(path.read_bytes())
                blob.extend(b"\n")
        blob.extend(json.dumps(self.gateway.log).encode())
        return bytes(blob)

    def restart_authority(self):
        self.store.close()
        self.store = self._open_store()
        self.gateway.store = self.store
        self.arch.invalidate_after_resume()
        self.dell.invalidate_after_resume()
        return self.store.status()

    def reboot_executor(self, device):
        revived = self.executors()[device].reopen()
        if device == "dell":
            self.dell_exec = revived
        else:
            self.arch_exec = revived
        self.ingress.executors[device] = revived
        return revived

    def capture(self, fn):
        try:
            return {"ok": True, "value": fn()}
        except IntegrationError as exc:
            return {"ok": False, "code": exc.code, "http": exc.http, "retry_after": exc.retry_after}
