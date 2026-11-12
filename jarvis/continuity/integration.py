"""Legacy/v2 bridge for MAIN continuity.

Production behavior is unchanged legacy. `ContinuityIntegration` does not dispatch
to the hub, the phone listener, or the CLI, and it refuses cutover while the
deployment gates in OPERATIONAL-CONTRACT.md are open.

`IsolatedFleet` builds a temp-directory fleet from the real modules: `AuthorityStore`
behind `ContinuityGateway` on a loopback socket, owner and installation ssh keys,
`AuthorityClient`, `MainRuntime`, `ExecutorGuard`, `ContinuityGatewaySession` and
`MobileIngress`. Only the MAIN relay sink and the clock are stand-ins. It is not an
Oracle or Dell deployment. Nothing here writes `jarvis.config.json`, `.jarvis/` or a
vault, and nothing starts at import time.

The module-level `close_unfenced` / `refuse_unfenced` helpers are the opt-in launch
contract. They are default-off RAM flags: legacy entry points call `refuse_unfenced`
and it does nothing until a deployment explicitly calls `close_unfenced`.
"""

import hashlib
import base64
import importlib
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .authority import (
    LEASE_SECONDS,
    SAFETY_MARGIN,
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
    verify_message,
)
from .ingress import READ_KINDS, ContinuityGatewaySession, MobileIngress, pin_target
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
    "target_unfenced", "unknown_participant")}, **{
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


def operation_digest(target, operation_id, attempt_id, effect_class):
    raw = json.dumps([target, operation_id, attempt_id, effect_class], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


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
            monotonic=clock.mono, wall=clock.wall)
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
            monotonic=self.clock.mono, wall=self.clock.wall)
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
        self.ingress = MobileIngress(
            path, session, origin=origin, installation_id="oracle-inst", allowed_mains=allowed,
            peer_verifier=peer_verifier, blocked_stable_ids=blocked_stable_ids,
            passkey_verifier=passkey_verifier, owner_verifier=owner_verifier, owner_proof=owner_proof,
            clock=self.clock.wall, log=log)
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
