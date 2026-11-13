"""Legacy/v2 bridge for MAIN continuity.

Production behavior is unchanged legacy. `ContinuityIntegration` does not dispatch
to the hub, the phone listener, or the CLI, and it refuses cutover while the
deployment gates in OPERATIONAL-CONTRACT.md are open.

`IsolatedFleet` builds a temp-directory fleet from the real modules: `AuthorityStore`
behind `ContinuityGateway` on a loopback socket, owner and installation ssh keys,
`AuthorityClient`, `MainRuntime`, `ExecutorGuard`, `ContinuityGatewaySession` and
`MobileIngress`. Only the MAIN relay sink and the clock are stand-ins. It is not an
Oracle or Dell deployment. Nothing here writes `jarvis.config.json` or a vault,
and nothing starts at import time. `configure_production` writes only the managed
marker in the state directory it is given.

The module-level `close_unfenced` / `refuse_unfenced` helpers are the opt-in launch
contract. They are default-off RAM flags: legacy entry points call `refuse_unfenced`
and it does nothing until a deployment explicitly calls `close_unfenced`.

`configure_production` is the other explicit enrollment. It binds a runtime the caller
already built and writes `continuity-managed.json` in that state directory. It does not
create an authority. An optional `issuer` signs one admission per effect; a missing
issuer refuses. With no marker, legacy calls stay as they are. Once the marker is on
disk, a process with no binding refuses before any effect. A managed hub job carries
that admission inside the payload, so the existing job signature covers it and another
process can read it from the broker.
"""

import contextvars
import hashlib
import base64
import importlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..fsio import atomic_write

from .authority import (
    MAX_SIGNATURE,
    TICKET_FIELDS,
    AuthorityError,
    AuthorityStore,
)
from .clock import suspend_aware_clock
from .executor import ExecutorGuard
from .gateway import (
    RELAY_PATH,
    TICKET_NS,
    AuthenticatedPort,
    AuthorityClient,
    ContinuityGateway,
    GatewayError,
    HTTP_STATUS as GATEWAY_HTTP_STATUS,
    _message,
    sign_message,
    verify_message,
)
from .ingress import (
    READ_KINDS,
    ContinuityGatewaySession,
    EnvelopeSigner,
    MobileIngress,
    TicketSignatureVerifier,
    pin_target,
)
from .runtime import GatewayPort, MainRuntime, RuntimeRefusal, StoreAdapter

ORIGIN = "https://gateway.test.invalid"
FLEET_ID = "home"
AUTHORITY_ID = "oracle-auth"

PERSONAL = frozenset({
    "prompt", "body", "text", "content", "file", "filename", "path", "screenshot",
    "frame", "image", "cookie_value", "passkey", "secret", "token", "transcript",
    "workflow", "message", "input", "payload",
})

# Contract-named store codes. Extras still fail closed; they never become HTTP 200. Codes the
# gateway also speaks take its status (one meaning per code); the rest are integration-only.
CONTRACT_HTTP = {code: GATEWAY_HTTP_STATUS[code] for code in (
    "bad_request", "bad_ticket", "ticket_expired", "stale", "busy", "key_reused", "conflict",
    "not_quiet", "lease_live", "quarantined")}
EXTRA_HTTP = {**{code: GATEWAY_HTTP_STATUS[code] for code in (
    "writer_busy", "not_found", "ahead", "bad_edge", "unacked", "expired", "draining", "mode",
    "ineligible", "main_alive", "no_main", "noop", "not_ready", "bad_state", "no_admission",
    "target_unfenced", "unknown_participant", "installation_mismatch", "active_holder",
    "transition_member", "revoked")}, **{
    "envelope_unsigned": 401,
    "body_not_relayable": 503,
    "authority_unavailable": 503,
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
}}
HTTP_STATUS = {**CONTRACT_HTTP, **EXTRA_HTTP}
RETRY_AFTER = frozenset({"not_quiet", "lease_live"})
STEPS = frozenset({"drain", "fence", "grant", "complete", "cancel"})

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
     "detail": "jarvis cluster main still writes cluster.main_device and does not take a lease. A default-off gate refuses it only after an explicit cutover closes `cluster_cli.set_main`."},
    {"id": "hub_unfenced",
     "detail": "Hub.dispatch consults only a default-off gate before local or node effects; no lease is checked. A cutover closes `hub_dispatch`, `cluster_local` and `remote_call` by hand."},
    {"id": "sleep_unhooked",
     "detail": "rest.sleep has a default-off gate but still does not release a lease or invalidate CachedAuthority; wiring MainRuntime.release_before_suspend is part of the cutover."},
    {"id": "margin_unmeasured",
     "detail": "SAFETY_MARGIN is the 2 second placeholder. It is not a measured suspend margin."},
    {"id": "windows_clock",
     "detail": "Dell has no tested suspend-aware clock and must not cache MAIN validity."},
    {"id": "no_oracle_device",
     "detail": "No verified Oracle gateway host is enrolled. Arch and Dell are not a substitute."},
    {"id": "ai_profile",
     "detail": "Luna +5 and Grok 4.7 are routing intent only. A provider name is not ai_ready."},
)

_CONSUME = TICKET_FIELDS


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


def _behavior(name):
    """Run the real peer on a temp fixture. A peer that only has the right names fails here."""
    with tempfile.TemporaryDirectory(prefix="jarvis-continuity-probe-") as folder:
        folder = Path(folder)
        if name == "gateway":
            return callable(getattr(ContinuityGateway, "forward_transient", None))
        if name == "ingress":
            return callable(getattr(ContinuityGatewaySession, "relay_transient", None))
        if name == "runtime":
            store = AuthorityStore(folder / "a.sqlite", FLEET_ID, AUTHORITY_ID)
            try:
                runtime = MainRuntime(StoreAdapter(store), "probe", "probe-inst",
                                      lambda: {"control_ready": True}, clock=None)
                runtime.start()
                try:
                    runtime.require_authority("ordinary")
                except RuntimeRefusal as exc:
                    legacy = exc.code == "legacy"
                else:
                    legacy = False
                calls = []

                class Client:
                    device_id = "probe"
                    installation_id = "probe-inst"

                    def consume(self, *args, **kwargs):
                        calls.append(1)
                        return {}

                try:
                    GatewayPort(Client()).consume(target="probe", admission_id="adm-x")
                except (RuntimeRefusal, AuthorityError):
                    unsigned = not calls
                else:
                    unsigned = False
                return legacy and unsigned
            finally:
                store.close() if hasattr(store, "close") else None
        if name == "executor":
            guard = ExecutorGuard(folder / "e.sqlite", device_id="probe", installation_id="probe-inst",
                                  fleet_id=FLEET_ID, authority_id=AUTHORITY_ID, boot=lambda: "boot-1")
            try:
                ran = []
                guard.legacy_effect(lambda: ran.append(1))
                if ran != [1]:
                    return False
                try:
                    guard.accept({}, {}, lambda: ran.append(2))
                except AuthorityError:
                    return ran == [1]
                return False
            finally:
                guard.close()
    return False


def peer_report():
    """Import the peers and run each against a temp fixture. Names alone are not compatibility."""
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
        try:
            behaves = _behavior(name)
        except Exception:
            behaves = False
        if not behaves:
            missing.append("behavior")
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


def _ticket_fields(signed):
    return {key: signed[key] for key in _CONSUME}


class LinkedClient(AuthorityClient):
    """Real `AuthorityClient` whose link a test can cut. Nothing is faked above the wire."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.up = True
        self.calls = []

    def _send(self, path, obj):
        self.calls.append(path)
        if not self.up:
            raise GatewayError("unavailable")
        return super()._send(path, obj)

    def renew_count(self):
        return sum(1 for path in self.calls if path.endswith("/renew"))


class SignedAuthority:
    """`ExecutorGuard` authority over the real signed gateway.

    `ExecutorGuard` verifies the MAIN ticket signature through `verify` and then calls
    `consume` with plain fields. The gateway needs that signature too, so `verify`
    keeps it (RAM, keyed by admission id) and `consume` refuses anything it did not
    verify. The key for a MAIN comes from the owner-enrolled public key, never from
    the request.
    """

    def __init__(self, client, public_keys):
        self._port = AuthenticatedPort(client)
        self._keys = dict(public_keys)
        self._signed = {}
        self._lock = threading.Lock()

    def verify(self, signed):
        try:
            fields = _ticket_fields(signed)
            signature = signed["signature"]
            key = self._keys.get(signed["main"])
        except (KeyError, TypeError):
            return False
        if key is None or not isinstance(signature, str):
            return False
        if not verify_message(TICKET_NS, _message(TICKET_NS, fields), signature, key):
            return False
        with self._lock:
            self._signed[fields["admission_id"]] = (fields, signature)
        return True

    def consume(self, **fields):
        with self._lock:
            stored = self._signed.get(fields.get("admission_id"))
        if stored is None:
            raise AuthorityError("signature", "ticket was not verified against a MAIN signature")
        original, signature = stored
        for key in _CONSUME:
            left, right = original[key], fields.get(key)
            numeric = (isinstance(left, (int, float)) and not isinstance(left, bool)
                       and isinstance(right, (int, float)) and not isinstance(right, bool))
            if numeric and abs(left - right) <= 1e-6:
                continue
            if not numeric and left == right:
                continue
            raise AuthorityError("signature", "ticket fields differ from the signed ticket")
        return self._port.consume(**original, ticket_signature=signature)

    def admission(self, admission_id):
        try:
            return self._port.admission(admission_id)
        except AuthorityError as exc:
            if exc.code == "not_found":
                return None
            raise

    def status(self):
        return self._port.status()

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        return self._port.acknowledge(device_id, installation_id, mode_version, fenced_epoch, boot_id=boot_id)


_PC_CAPS = ["status", "lease", "fence", "candidate", "consume", "reconcile"]
_SERVER_CAPS = ["status", "fence", "reconcile"]
_REPLY_LIMIT = 4 * 1024 * 1024
_RELAY_KEYS = frozenset({
    "operation_id", "target", "attempt_id", "digest", "capability", "kind", "authority_id",
    "incarnation", "epoch", "main", "envelope_b64",
})


# Same shape as the Oracle gateway config's `self_recovery`: both PCs may recover
# MAIN for themselves once the old lease lapses; zenbook (Arch) takes it back.
SELF_RECOVERY = {"candidates": ["zenbook", "dell"], "preferred": "zenbook"}


class PcNode:
    """One PC: real runtime, real executor journal, and a temporary MAIN relay sink.

    The sink is a loopback test server standing in for MAIN's receiver. It is not a
    shipped listener; `jarvis.continuity` never starts one. Effects go through
    `require_authority`, then a freshly signed ticket, then `ExecutorGuard.accept`
    with a zero-argument sink. Personal text is held in RAM only long enough to run.
    """

    def __init__(self, fleet, name, *, suspend_cache):
        self.fleet = fleet
        self.name = name
        self.installation = name + "-inst"
        self.boot = {"id": "boot-" + name + "-1"}
        self.effects = []
        self.refusals = []
        self.client = fleet._client(LinkedClient, name)
        self.port = GatewayPort(self.client)
        self.authority = SignedAuthority(
            self.client, {"zenbook": fleet.public("zenbook"), "dell": fleet.public("dell")})
        clock = fleet.clock.mono if suspend_cache else None
        self.runtime = MainRuntime(self.port, name, self.installation, self._probe,
                                   clock=clock, boot=lambda: self.boot["id"])
        self.executor_path = fleet.root / name / "executor.sqlite"
        self.executor_path.parent.mkdir(parents=True, exist_ok=True)
        self.executor = self._open_executor()
        node = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                return

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.path != RELAY_PATH or not 0 < length <= _REPLY_LIMIT:
                        raise ValueError
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    status, reply = node.handle_relay(body)
                except Exception:
                    status, reply = 503, {"error": "unavailable"}
                raw = json.dumps(reply, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=False)
        self.thread.start()
        self.origin = "http://127.0.0.1:%d" % self.server.server_address[1]

    @staticmethod
    def _probe():
        return {"control_ready": True, "luna": True, "grok": True, "quota_ok": True,
                "mediated_tools": True, "session_local": True, "checkpoint": "cp-1"}

    def _open_executor(self):
        return ExecutorGuard(
            self.executor_path, device_id=self.name, installation_id=self.installation,
            fleet_id=FLEET_ID, authority_id=AUTHORITY_ID, authority=self.authority,
            verifier=self.authority.verify, boot=lambda: self.boot["id"], wall=self.fleet.clock.wall)

    def reboot(self):
        """Close the journal and reopen it under a new boot id. Nothing else is carried over."""
        self.executor.close()
        self.boot["id"] = "boot-%s-%d" % (self.name, int(self.boot["id"].rsplit("-", 1)[1]) + 1)
        self.executor = self._open_executor()
        return self.executor

    def handle_relay(self, body):
        if not isinstance(body, dict) or set(body) != _RELAY_KEYS:
            return 400, {"error": "bad_request"}
        envelope = base64.b64decode(body["envelope_b64"], validate=True)
        if body["digest"] != "sha256:" + hashlib.sha256(envelope).hexdigest():
            return 400, {"error": "bad_request"}
        if body["target"] != self.name or body["main"] != self.name or body["capability"] != "client":
            return 403, {"error": "forbidden"}
        kind, operation_id = body["kind"], body["operation_id"]
        if kind in READ_KINDS:
            try:
                self.runtime.require_authority("control", target=self.name)
            except RuntimeRefusal as exc:
                return 409, {"error": exc.code}
            return 200, self._read(operation_id, kind)
        try:
            auth = self.runtime.require_authority("ordinary", target=self.name)
        except RuntimeRefusal as exc:
            self.refusals.append(exc.code)
            return 200, {"operation_id": operation_id, "accepted": False}
        wall = self.fleet.clock.wall()
        ticket = {
            "admission_id": "adm-%s-%s" % (operation_id, body["attempt_id"]), "target": self.name,
            "operation_id": operation_id, "attempt_id": body["attempt_id"], "digest": body["digest"],
            "main": self.name, "incarnation": auth["incarnation"], "epoch": auth["epoch"],
            "issued_at": wall - 1, "admit_until": wall + 60, "issuer_incarnation": auth["incarnation"],
        }
        signed = {
            **ticket, "authority_id": AUTHORITY_ID, "fleet_id": FLEET_ID,
            "mode_version": auth["mode_version"], "policy_version": auth["policy_version"],
            "effect_class": "ordinary", "signature": self.client.sign_ticket(ticket),
        }
        try:
            receipt = self.executor.accept(signed, ticket, lambda: self.effects.append(operation_id))
        except AuthorityError as exc:
            self.refusals.append(exc.code)
            return 200, {"operation_id": operation_id, "accepted": False}
        return 200, {"operation_id": operation_id, "accepted": receipt.get("effect") == "completed"}

    def _read(self, operation_id, kind):
        if kind == "session_frame":
            raw = base64.b64encode(b"\x89frame-" + self.name.encode()).decode("ascii")
            return {"operation_id": operation_id, "raw_b64": raw, "result": {
                "status": 200, "body": {},
                "headers": {"X-Frame-Seq": "1", "X-Frame-Width": "2", "X-Frame-Height": "2"}}}
        return {"operation_id": operation_id, "result": {
            "status": 200, "body": {"main": self.name, "state": "ready"}, "headers": {}}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.executor.close()


class IsolatedFleet:
    """Temp-directory fleet built from the real modules and nothing else.

    Real: `AuthorityStore` behind `ContinuityGateway` on a loopback socket, installation
    and owner ssh keys, `AuthorityClient`, `MainRuntime`, `ExecutorGuard`,
    `ContinuityGatewaySession`, `MobileIngress`. Stand-ins: a loopback MAIN relay sink per
    PC, and a `Clock`. It refuses a root under the live `.jarvis`. It is not Oracle or Dell.
    """

    def __init__(self, directory, clock):
        root = Path(directory).resolve()
        live = (Path(__file__).resolve().parents[2] / ".jarvis").resolve()
        if root == live or live in root.parents:
            raise IntegrationError("refuses_live_state")
        self.root = root
        self.clock = clock
        self._keys = self._make_keys()
        self.gateway = ContinuityGateway.open(
            root / "authority.sqlite", FLEET_ID, AUTHORITY_ID, [self._keys["owner"][0]],
            monotonic=clock.mono, wall=clock.wall,
            self_recovery=SELF_RECOVERY)
        self.origin = self.gateway.serve()
        self.owner = self._client(LinkedClient, None, owner=True)
        self._enrolled = []
        self.nodes = {}
        self.ingress = None
        try:
            self.nodes["zenbook"] = PcNode(self, "zenbook", suspend_cache=True)
            self.nodes["dell"] = PcNode(self, "dell", suspend_cache=False)
        except Exception:
            self.close()
            raise
        self.oracle = self._client(LinkedClient, "oracle")
        self.phone_client = self._client(LinkedClient, "ingress")

    def _make_keys(self):
        folder = self.root / "keys"
        folder.mkdir(parents=True, exist_ok=True)
        made = {}
        for name in ("owner", "zenbook", "dell", "oracle", "ingress"):
            path = folder / name
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-continuity-test", "-f", str(path)],
                check=True, capture_output=True, timeout=20)
            token = (folder / (name + ".pub")).read_text(encoding="ascii").split()
            made[name] = (token[0] + " " + token[1], path)
        return made

    def public(self, name):
        return self._keys[name][0]

    def status(self):
        """Metadata status as the Arch PC's own authenticated client sees it."""
        return self.nodes["zenbook"].client.status()

    def _client(self, cls, name, *, owner=False):
        kwargs = {"authority_id": AUTHORITY_ID, "wall": self.clock.wall}
        if name is not None:
            kwargs.update(device_id=name, installation_id=name + "-inst",
                          private_key_path=self._keys[name][1])
        if owner or name is None:
            kwargs["owner_private_key_path"] = self._keys["owner"][1]
        return cls(self.origin, **kwargs)

    def enroll(self, name, kind, capabilities, relay_origin=None):
        record = {"device_id": name, "installation_id": name + "-inst", "kind": kind,
                  "public_key": self.public(name), "capabilities": list(capabilities)}
        if relay_origin:
            record["relay_origin"] = relay_origin
        self._enrolled.append(record)
        return self.owner.enroll(record)

    def restart_gateway(self):
        """Close the gateway and reopen it on the same database, a new port and fresh RAM.

        Enrolments live in the gateway's RAM, so the owner re-enrols them. The authority
        store keeps its rows: it comes back with a new incarnation and no MAIN.
        """
        self.gateway.close()
        self.gateway = ContinuityGateway.open(
            self.root / "authority.sqlite", FLEET_ID, AUTHORITY_ID, [self._keys["owner"][0]],
            monotonic=self.clock.mono, wall=self.clock.wall,
            self_recovery=SELF_RECOVERY)
        self.origin = self.gateway.serve()
        clients = [self.owner, self.oracle, self.phone_client] + [n.client for n in self.nodes.values()]
        for client in clients:
            client.origin = self.origin
        for record in self._enrolled:
            self.owner.enroll(record)
        if self.ingress is not None:
            self.ingress._gateway._gateway = self.gateway
        return self.nodes["zenbook"].client.status()

    def cutover(self):
        """Enroll everyone and walk the store to V2_ONLY_NO_MAIN. Every participant acks."""
        for name, node in self.nodes.items():
            self.enroll(name, "pc", _PC_CAPS, relay_origin=node.origin)
        self.enroll("oracle", "server", _SERVER_CAPS)
        self.enroll("ingress", "client", ["status", "client"])
        status = self.nodes["zenbook"].client.status()
        version = status["mode_version"]
        version = self.owner.set_mode("PREPARING_V2", version)["mode_version"]
        version = self.owner.set_mode("QUIESCED", version)["mode_version"]
        for node in self.nodes.values():
            node.executor.fence(status["incarnation"], 0, version + 1)
        self.oracle.acknowledge(version + 1, 0)
        self.owner.set_mode("V2_ONLY_NO_MAIN", version)
        return self.nodes["zenbook"].client.status()

    def promote(self, candidate, key=None, *, canary=True, until="complete"):
        """Owner-driven recover to `candidate`. `key` is the request idempotency id.

        `until` stops after drain/fence/grant for failure tests.
        """
        key = key or "recover-" + candidate + "-" + str(int(self.clock.wall()))
        node = self.nodes[candidate]
        status = node.client.status()
        opened = self.owner.transition_request(
            key, "recover", candidate, status["incarnation"], status["epoch"], status["policy_version"])
        tid = opened["transition_id"]
        node.client.readiness(tid, True, False, "cp-1")
        self.owner.step(tid, "drain")
        if until == "drain":
            return tid, None
        while True:
            try:
                self.owner.step(tid, "fence")
                break
            except GatewayError as exc:
                if exc.code != "not_quiet" or not exc.wait:
                    raise
                self.clock.tick(exc.wait + 0.01)
        if until == "fence":
            return tid, None
        lease = self.owner.step(tid, "grant")
        if until == "grant":
            return tid, lease
        self.owner.step(tid, "complete", canary)
        return tid, lease

    def phone_ingress(self, path, *, peer_verifier, owner_verifier=None, owner_proof=None,
                      passkey_verifier=None, blocked_stable_ids=("pc-zenbook", "pc-dell"), log=None,
                      origin=ORIGIN):
        session = ContinuityGatewaySession(
            self.gateway, "ingress", "ingress-inst", self._keys["ingress"][1], wall=self.clock.wall)
        allowed = {
            "zenbook": pin_target("zenbook", "https://zenbook.tailnet.ts.net", "100.64.0.10"),
            "dell": pin_target("dell", "https://dell.tailnet.ts.net", "100.64.0.11"),
        }
        # Same objects production_ingress requires. The ingress key signs the phone
        # claim; each PC public key checks that MAIN's admission ticket.
        self.ingress = MobileIngress(
            path, session, origin=origin, installation_id="oracle-inst", allowed_mains=allowed,
            peer_verifier=peer_verifier, blocked_stable_ids=blocked_stable_ids,
            passkey_verifier=passkey_verifier, owner_verifier=owner_verifier, owner_proof=owner_proof,
            clock=self.clock.wall, log=log,
            envelope_signer=EnvelopeSigner(self._keys["ingress"][1]),
            ticket_verifier=TicketSignatureVerifier({
                "zenbook": self.public("zenbook"), "dell": self.public("dell"),
            }))
        return self.ingress

    def tickets(self, name):
        return self.nodes[name].executor

    def persisted_bytes(self):
        blobs = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.parent.name != "keys":
                blobs.append(path.read_bytes())
        blobs.append(json.dumps(self.gateway.audit_events()).encode("utf-8"))
        return b"\n".join(blobs)

    def close(self):
        if self.ingress is not None:
            try:
                self.ingress.close()
            except Exception:
                pass
        for node in self.nodes.values():
            node.close()
        try:
            self.gateway.close()
        except Exception:
            pass


# Opt-in launch contract. Everything here is RAM and default-off: with no call to
# `close_unfenced`, every legacy entry point behaves as before.
_launch = {"closed": set(), "seen": "before", "phone_open": True}


def close_unfenced(*surfaces):
    """Close legacy surfaces. Called only by a deployment that has done the explicit cutover."""
    _launch["closed"].update(str(item) for item in (surfaces or ("*",)))


def open_unfenced():
    _launch["closed"].clear()
    _launch["seen"] = "before"
    _launch["phone_open"] = True


def refuse_unfenced(surface):
    """Legacy entry-point hook: raise only after an explicit `close_unfenced`."""
    if "*" in _launch["closed"] or surface in _launch["closed"]:
        raise IntegrationError("legacy_retained")


def node_seen_mode():
    return _launch["seen"]


def set_node_seen_mode(mode):
    if mode not in ("before", "after"):
        raise IntegrationError("bad_request")
    _launch["seen"] = mode


def apply_node_seen(record, run):
    """Run a node job with the seen-mark before (legacy) or after (v2, so a failed run is retried)."""
    if _launch["seen"] == "after":
        result = run()
        record()
        return result
    record()
    return run()


def phone_listener_open():
    return _launch["phone_open"] and "phone" not in _launch["closed"] and "*" not in _launch["closed"]


def set_phone_listener_open(value):
    _launch["phone_open"] = bool(value)


# Production enrollment. The marker is durable; the binding is this process only.
# A restart keeps the marker and drops the binding, and the next effect refuses.
_MARKER_NAME = "continuity-managed.json"
_MARKER_KEYS = frozenset({"managed", "schema"})
_production_lock = threading.Lock()
_production = {}
# One verified local effect. Set only inside an ExecutorGuard sink, and only the
# matching execute_local may pass. It is not a device-wide bypass.
_bridge = contextvars.ContextVar("jarvis_continuity_local_bridge", default=None)
ENVELOPE_KEY = "continuity_envelope"
_ENVELOPE_FIELDS = frozenset({"schema", "kind", "signed_operation", "admission_ticket"})
_SIGNED_FIELDS = (
    "authority_id", "fleet_id", "main", "incarnation", "epoch", "mode_version",
    "policy_version", "target", "operation_id", "attempt_id", "digest", "effect_class",
    "issued_at", "admit_until", "issuer_incarnation", "admission_id", "signature",
)
_EFFECTS = frozenset(("ordinary", "pure_compute", "interactive"))
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_WHOLE = ("incarnation", "epoch", "mode_version", "policy_version", "issuer_incarnation",
          "issued_at", "admit_until")
_ADMISSION_WINDOW = 60


def _state_dir(state):
    return Path(state).expanduser().resolve()


def _marker_path(state):
    return _state_dir(state) / _MARKER_NAME


def _directory_sync(descriptor):
    os.fsync(descriptor)


def _fsync_directory(directory):
    """Durably link the marker's directory entry. Windows cannot fsync a directory."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        if sys.platform != "win32":
            raise
        return
    try:
        try:
            _directory_sync(descriptor)
        except OSError:
            if sys.platform != "win32":
                raise
    finally:
        os.close(descriptor)


def continuity_state(state):
    """'legacy' when the marker is absent, 'managed' when it is a valid enrollment file.

    Only a missing file is unmanaged. A symlink, directory, unreadable file, or malformed
    document is quarantined and never treated as legacy.
    """
    path = _marker_path(state)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "legacy"
    except OSError as exc:
        raise IntegrationError("quarantined") from exc
    if stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise IntegrationError("quarantined")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "legacy"
    except OSError as exc:
        raise IntegrationError("quarantined") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeError) as exc:
        raise IntegrationError("quarantined") from exc
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise IntegrationError("quarantined") from exc
    if (not isinstance(loaded, dict) or set(loaded) != _MARKER_KEYS
            or loaded.get("managed") is not True or type(loaded.get("schema")) is not int
            or loaded.get("schema") != 1):
        raise IntegrationError("quarantined")
    return "managed"


def _managed(state):
    """True once this state directory has been explicitly enrolled. Quarantine propagates."""
    return continuity_state(state) == "managed"


def _write_marker(directory):
    """Atomic, durable, secret-free enrollment flag. Replaces the whole file."""
    path = directory / _MARKER_NAME
    payload = {"managed": True, "schema": 1}
    atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")),
                 mode=0o600, fsync=True)
    _fsync_directory(directory)
    if continuity_state(directory) != "managed":
        raise IntegrationError("bad_request")
    return path


def _bound(state):
    with _production_lock:
        found = _production.get(str(_state_dir(state)))
        return None if found is None else dict(found)


def configure_production(state, runtime, executor=None, handoff=None, issuer=None):
    """Bind an already-built runtime for `state` and record managed enrollment.

    Does not construct an authority, a client, a lease, or a guard. `runtime` must
    already expose `require_authority` and `release_before_suspend`. `executor` is the
    node's `ExecutorGuard` when this process runs fenced jobs. `handoff` is stored for
    the later switch coordinator and is not called. `issuer` signs one admission per
    effect. It is not called here and is not written to disk. A missing issuer refuses
    at the effect, not at enrollment.
    """
    if runtime is None or not callable(getattr(runtime, "require_authority", None)):
        raise IntegrationError("bad_request")
    if not callable(getattr(runtime, "release_before_suspend", None)):
        raise IntegrationError("bad_request")
    if executor is not None and not callable(getattr(executor, "accept", None)):
        raise IntegrationError("bad_request")
    if issuer is not None and not callable(issuer):
        raise IntegrationError("bad_request")
    directory = _state_dir(state)
    directory.mkdir(parents=True, exist_ok=True)
    _write_marker(directory)
    with _production_lock:
        _production[str(directory)] = {
            "runtime": runtime, "executor": executor, "handoff": handoff, "issuer": issuer}
    return production_binding(directory)


def clear_production(state=None):
    """Drop the in-process binding. The durable marker is left in place."""
    with _production_lock:
        if state is None:
            _production.clear()
            return None
        _production.pop(str(_state_dir(state)), None)
    return None


def production_binding(state):
    """The in-process binding, or None. Never reads the marker as a substitute."""
    return _bound(state)


def require_production_authority(state, config, surface, target=None):
    """Gate one legacy effect. Unmanaged state returns without calling the runtime.

    `config` is not an authority source. A payload flag cannot satisfy this check.
    Managed state with no bound runtime refuses, including after a process restart.
    """
    del config
    if not _managed(state):
        return None
    if not isinstance(surface, str) or not surface:
        raise IntegrationError("bad_request")
    binding = _bound(state)
    runtime = None if binding is None else binding.get("runtime")
    require = getattr(runtime, "require_authority", None) if runtime is not None else None
    if not callable(require):
        raise IntegrationError("managed_unbound")
    if target is not None and not isinstance(target, str):
        raise IntegrationError("bad_request")
    return require("ordinary", target)


def _ident_ok(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _whole_ok(value):
    return type(value) is int and value >= 0


def _device_of(config):
    from jarvis.topology import identity
    try:
        local = identity(config)[0]
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        raise IntegrationError("bad_request") from None
    if not _ident_ok(local):
        raise IntegrationError("bad_request")
    return local


def _guard_device(guard):
    if guard is None or not callable(getattr(guard, "accept", None)):
        return None
    device = getattr(guard, "device_id", None)
    if not _ident_ok(device):
        return None
    return device


def effect_digest(kind, payload):
    """Canonical digest of one effect. The admission envelope is not part of the effect."""
    if not isinstance(kind, str) or not kind or not isinstance(payload, dict) or ENVELOPE_KEY in payload:
        raise IntegrationError("bad_request")
    try:
        raw = json.dumps({"kind": kind, "payload": payload}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IntegrationError("bad_request") from exc
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


def _require_issued(signed, ticket, *, target, digest, effect_class, operation_id=None, attempt_id=None):
    """The issuer's pair must be exactly the admission that was asked for. No extra keys."""
    if not isinstance(signed, dict) or not isinstance(ticket, dict):
        raise IntegrationError("bad_request")
    if set(signed) != set(_SIGNED_FIELDS) or set(ticket) != set(TICKET_FIELDS):
        raise IntegrationError("bad_request")
    if effect_class not in _EFFECTS or signed.get("effect_class") != effect_class:
        raise IntegrationError("bad_request")
    if not _DIGEST.fullmatch(digest or "") or signed.get("digest") != digest or ticket.get("digest") != digest:
        raise IntegrationError("bad_request")
    if ticket.get("target") != target or signed.get("target") != target or not _ident_ok(target):
        raise IntegrationError("bad_request")
    for key in TICKET_FIELDS:
        if signed.get(key) != ticket.get(key):
            raise IntegrationError("bad_request")
    if operation_id is not None and ticket.get("operation_id") != operation_id:
        raise IntegrationError("bad_request")
    if attempt_id is not None and ticket.get("attempt_id") != attempt_id:
        raise IntegrationError("bad_request")
    for key in ("admission_id", "operation_id", "attempt_id", "main", "authority_id", "fleet_id"):
        if not _ident_ok(signed.get(key)):
            raise IntegrationError("bad_request")
    for key in _WHOLE:
        if not _whole_ok(signed.get(key)):
            raise IntegrationError("bad_request")
    signature = signed.get("signature")
    if not isinstance(signature, str) or not 1 <= len(signature) <= MAX_SIGNATURE:
        raise IntegrationError("bad_request")
    if signed["admit_until"] <= signed["issued_at"]:
        raise IntegrationError("bad_request")
    if signed["admit_until"] - signed["issued_at"] > 24 * 3600:
        raise IntegrationError("bad_request")
    return signed, ticket


def _issue(issuer, target, digest, effect_class):
    operation_id = "op-" + secrets.token_hex(8)
    attempt_id = "at-" + secrets.token_hex(8)
    issued = issuer(target, operation_id, attempt_id, digest, effect_class)
    if not isinstance(issued, tuple) or len(issued) != 2:
        raise IntegrationError("bad_request")
    return _require_issued(
        issued[0], issued[1], target=target, digest=digest, effect_class=effect_class,
        operation_id=operation_id, attempt_id=attempt_id)


def production_issuer(private_key, client, *, wall=None):
    """Sign admissions with an explicit MAIN key. `client.status()` supplies the tuple.

    The callable does not verify tickets and does not treat a boolean as a signature.
    A MAIN that is not current and ready refuses with `stale` before anything is signed.
    """
    clock = time.time if wall is None else wall
    if not callable(clock):
        raise IntegrationError("bad_request")

    def issue(target, operation_id, attempt_id, digest, effect_class):
        try:
            status = client.status()
        except GatewayError as exc:
            raise IntegrationError("managed_unbound") from exc
        if not isinstance(status, dict):
            raise IntegrationError("stale")
        ready = (status.get("mode") == "V2_ACTIVE" and status.get("lease_valid") is True
                 and status.get("main_ready") is True and _ident_ok(status.get("main")))
        if not ready or effect_class not in _EFFECTS:
            raise IntegrationError("stale" if effect_class in _EFFECTS else "bad_request")
        if not _ident_ok(target) or not _ident_ok(operation_id) or not _ident_ok(attempt_id):
            raise IntegrationError("bad_request")
        if not _DIGEST.fullmatch(digest or ""):
            raise IntegrationError("bad_request")
        if not _ident_ok(status.get("authority_id")) or not _ident_ok(status.get("fleet_id")):
            raise IntegrationError("stale")
        numbers = {}
        for key in ("incarnation", "epoch", "mode_version", "policy_version"):
            value = status.get(key)
            if not _whole_ok(value):
                raise IntegrationError("stale")
            numbers[key] = value
        now = clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise IntegrationError("stale")
        issued_at = int(now)
        ticket = {
            "admission_id": "adm-" + operation_id,
            "target": target,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "digest": digest,
            "main": status["main"],
            "incarnation": numbers["incarnation"],
            "epoch": numbers["epoch"],
            "issued_at": issued_at,
            "admit_until": issued_at + _ADMISSION_WINDOW,
            "issuer_incarnation": numbers["incarnation"],
        }
        signed = {
            "authority_id": status["authority_id"],
            "fleet_id": status["fleet_id"],
            "main": ticket["main"],
            "incarnation": ticket["incarnation"],
            "epoch": ticket["epoch"],
            "mode_version": numbers["mode_version"],
            "policy_version": numbers["policy_version"],
            "target": target,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "digest": digest,
            "effect_class": effect_class,
            "issued_at": issued_at,
            "admit_until": ticket["admit_until"],
            "issuer_incarnation": ticket["issuer_incarnation"],
            "admission_id": ticket["admission_id"],
        }
        try:
            signed["signature"] = sign_message(TICKET_NS, private_key, _message(TICKET_NS, {
                key: ticket[key] for key in TICKET_FIELDS}))
        except GatewayError as exc:
            raise IntegrationError("managed_unbound") from exc
        return _require_issued(
            signed, ticket, target=target, digest=digest, effect_class=effect_class,
            operation_id=operation_id, attempt_id=attempt_id)

    return issue


def seal_job_payload(state, target, kind, payload, effect_class="ordinary"):
    """Return the payload to enqueue. Managed state nests a signed admission envelope.

    The caller's dict is not mutated. Unmanaged state is returned unchanged. The
    envelope is inside the payload so the existing job signature covers it.
    """
    if continuity_state(state) != "managed":
        return payload
    if not isinstance(payload, dict) or ENVELOPE_KEY in payload or not _ident_ok(target):
        raise IntegrationError("bad_request")
    if effect_class not in _EFFECTS:
        raise IntegrationError("bad_request")
    binding = _bound(state)
    issuer = None if binding is None else binding.get("issuer")
    if not callable(issuer):
        raise IntegrationError("managed_unbound")
    digest = effect_digest(kind, payload)
    signed, ticket = _issue(issuer, target, digest, effect_class)
    wrapped = dict(payload)
    wrapped[ENVELOPE_KEY] = {
        "schema": 1,
        "kind": kind,
        "signed_operation": signed,
        "admission_ticket": ticket,
    }
    return wrapped


def _open_envelope(job, device):
    """Strip a verified job's envelope. Digest, kind, and target must match before the guard."""
    if not isinstance(job, dict):
        raise IntegrationError("bad_request")
    kind = job.get("kind")
    payload = job.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict) or ENVELOPE_KEY not in payload:
        raise IntegrationError("managed_unbound")
    envelope = payload.get(ENVELOPE_KEY)
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise IntegrationError("bad_request")
    if type(envelope.get("schema")) is not int or envelope.get("schema") != 1:
        raise IntegrationError("bad_request")
    if envelope.get("kind") != kind:
        raise IntegrationError("bad_request")
    stripped = {key: value for key, value in payload.items() if key != ENVELOPE_KEY}
    digest = effect_digest(kind, stripped)
    signed = envelope.get("signed_operation")
    ticket = envelope.get("admission_ticket")
    effect_class = signed.get("effect_class") if isinstance(signed, dict) else None
    if effect_class not in _EFFECTS:
        raise IntegrationError("bad_request")
    _require_issued(signed, ticket, target=device, digest=digest, effect_class=effect_class)
    return stripped, signed, ticket, digest


def _bridge_matches(bridge, state, config, kind, digest):
    if not isinstance(bridge, dict) or bridge.get("used") is not False:
        return False
    try:
        device = _device_of(config)
    except IntegrationError:
        return False
    return (bridge.get("state") == str(_state_dir(state)) and bridge.get("device") == device
            and bridge.get("kind") == kind and bridge.get("digest") == digest
            and _ident_ok(bridge.get("operation_id")) and _ident_ok(bridge.get("attempt_id")))


def local_desktop_args():
    """Tools and config the managed desktop bridge may pass into `run_here`. Empty otherwise."""
    bridge = _bridge.get()
    desktop = bridge.get("desktop") if isinstance(bridge, dict) else None
    if not isinstance(desktop, dict):
        return {}
    return {key: desktop[key] for key in ("tools", "config", "state") if key in desktop}


def _admit_local(state, config, device, kind, payload, digest, *, desktop=None):
    """One guard admission, then exactly one matching `execute_local`."""
    require_production_authority(state, config, "cluster_local", target=device)
    binding = _bound(state)
    issuer = None if binding is None else binding.get("issuer")
    guard = None if binding is None else binding.get("executor")
    if not callable(issuer) or _guard_device(guard) is None:
        raise IntegrationError("managed_unbound")
    if guard.device_id != device:
        raise IntegrationError("managed_unbound")
    signed, ticket = _issue(issuer, device, digest, "ordinary")
    captured = {}

    def sink():
        record = {
            "state": str(_state_dir(state)),
            "device": device,
            "kind": kind,
            "digest": digest,
            "operation_id": ticket["operation_id"],
            "attempt_id": ticket["attempt_id"],
            "used": False,
            "desktop": desktop if isinstance(desktop, dict) else None,
        }
        token = _bridge.set(record)
        try:
            from jarvis.cluster import execute_local
            captured["result"] = execute_local(kind, payload, config, state)
        finally:
            _bridge.reset(token)

    guard.accept(signed, ticket, sink)
    if "result" not in captured:
        raise IntegrationError("not_run")
    return captured["result"]


def open_local_effect(state, config, kind, payload, run_body):
    """Run `run_body` once. Managed state admits it through the bound guard first.

    A bridge that does not match this state, device, kind, and payload digest, or that
    was already used, refuses. Checks that decide admission stay outside the guard.
    """
    if not callable(run_body):
        raise IntegrationError("bad_request")
    bridge = _bridge.get()
    if bridge is not None:
        digest = effect_digest(kind, payload)
        if _bridge_matches(bridge, state, config, kind, digest):
            bridge["used"] = True
            return run_body()
        raise IntegrationError("bridge_refused")
    if continuity_state(state) == "legacy":
        return run_body()
    digest = effect_digest(kind, payload)
    return _admit_local(state, config, _device_of(config), kind, payload, digest)


def run_managed_local(state, config, target, kind, payload, *, desktop=None):
    """Admit one local effect and run it through `execute_local`. Unmanaged state is not this path."""
    if continuity_state(state) != "managed":
        raise IntegrationError("bad_request")
    device = _device_of(config)
    if target != device:
        raise IntegrationError("bad_request")
    digest = effect_digest(kind, payload)
    return _admit_local(state, config, device, kind, payload, digest, desktop=desktop)


def managed_node_disposition(state, job):
    """'legacy' when unmanaged, 'admit' when the payload envelope binds this guard, else 'refuse'.

    Top-level tickets and payload copies that are not the reserved envelope do not admit.
    A quarantined marker refuses. It is not a legacy job.
    """
    try:
        if continuity_state(state) != "managed":
            return "legacy"
    except IntegrationError:
        return "refuse"
    binding = _bound(state)
    guard = None if binding is None else binding.get("executor")
    device = _guard_device(guard)
    if device is None or not isinstance(job, dict):
        return "refuse"
    try:
        _open_envelope(job, device)
    except IntegrationError:
        return "refuse"
    return "admit"


def run_admitted_node_job(state, job, executor):
    """`ExecutorGuard.accept` then the captured executor result. The guard's return is not the effect.

    The payload the executor sees has had the envelope removed. The bridge lets the
    default `execute_local` run that stripped payload once, without a second admission.
    """
    if managed_node_disposition(state, job) != "admit":
        raise IntegrationError("managed_unbound")
    guard = _bound(state)["executor"]
    device = _guard_device(guard)
    stripped, signed, ticket, digest = _open_envelope(job, device)
    kind = job["kind"]
    captured = {}

    def sink():
        record = {
            "state": str(_state_dir(state)),
            "device": device,
            "kind": kind,
            "digest": digest,
            "operation_id": ticket["operation_id"],
            "attempt_id": ticket["attempt_id"],
            "used": False,
            "desktop": None,
        }
        token = _bridge.set(record)
        try:
            captured["result"] = executor(kind, stripped)
        finally:
            _bridge.reset(token)

    guard.accept(signed, ticket, sink)
    if "result" not in captured:
        # The guard returned without running the sink. Do not invent a result or run it here.
        raise IntegrationError("not_run")
    return captured["result"]


def _release_succeeded(report):
    """A dropped lease, or a runtime that held none and stopped admission locally."""
    if not isinstance(report, dict):
        return False
    if report.get("released") is True:
        return True
    return (report.get("code") == "no_lease" and report.get("admission_stopped") is True
            and report.get("invalidated") is True)


def _release_refusal(report):
    code = report.get("code") if isinstance(report, dict) else None
    plain = code.replace("_", "") if isinstance(code, str) else ""
    if (isinstance(code, str) and code != "released" and code.islower()
            and 1 <= len(code) <= 40 and plain.isalpha()):
        return code
    return "not_released"


def release_for_suspend(state):
    """Release the bound MAIN lease before `rest.pause`. Unmanaged state does nothing.

    A missing runtime, or a release that did not drop the lease, refuses. `no_lease`
    is success: this process held nothing, and the runtime has stopped local admission.
    """
    if not _managed(state):
        return None
    binding = _bound(state)
    runtime = None if binding is None else binding.get("runtime")
    release = getattr(runtime, "release_before_suspend", None) if runtime is not None else None
    if not callable(release):
        raise IntegrationError("managed_unbound")
    report = release()
    if not _release_succeeded(report):
        raise IntegrationError(_release_refusal(report))
    return report
