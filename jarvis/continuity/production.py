"""Production composition for the continuity process adapters.

This module wires the existing protocol pieces. It does not manufacture readiness,
keys, routes, enrolled pins, or owner proof. A missing prerequisite is a refusal.
"""
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import SafeHandoff, RuntimeRefusal
from .participant import CandidatePort, RemoteRuntime


def _pins(operator):
    enrollment = operator.get("enrollment") if isinstance(operator, dict) else None
    if not isinstance(enrollment, dict) or not enrollment:
        raise RuntimeRefusal("enrollment_pins_missing")
    pins = {}
    for device, record in enrollment.items():
        if not isinstance(record, dict) or record.get("kind") not in ("pc", "client", "server"):
            raise RuntimeRefusal("enrollment_pins_missing")
        if record.get("kind") != "pc":
            continue
        if (not isinstance(device, str) or not device
                or not isinstance(record.get("public_key"), str)
                or not record["public_key"].strip()
                or not isinstance(record.get("installation_id"), str)
                or not record["installation_id"].strip()):
            raise RuntimeRefusal("enrollment_pins_missing")
        pins[device] = {"public_key": record["public_key"],
                        "installation_id": record["installation_id"],
                        "kind": record.get("kind")}
    return pins


BRAIN_OUTCOME = "brainsync-last-handoff.json"
BRAIN_OUTCOME_SCHEMA = "jarvis-brain-handoff-outcome/1"


def _record_brain_outcome(base, old, new, result, transport):
    """Write the last managed brain step as codes only (no paths, names or memory).

    The SafeHandoff journal keeps only ok/false for the step. This file says which
    stage failed and why. It is diagnostics: a write failure never changes the step.
    """
    import json
    import time
    from jarvis import brainsync
    from .brain_channel import safe_code
    try:
        out = {"schema": BRAIN_OUTCOME_SCHEMA, "at": int(time.time()),
               "old_main": safe_code(old, None), "new_main": safe_code(new, None)}
        if isinstance(result, BaseException):
            out.update(ok=False, raised=safe_code(type(result).__name__.lower()),
                       code=safe_code(getattr(result, "code", None), None),
                       remote_code=safe_code(getattr(result, "remote_code", None), None))
        elif isinstance(result, dict):
            out.update(ok=result.get("ok") is True, status=safe_code(result.get("status"), None),
                       error=safe_code(result.get("error"), None))
        else:
            out.update(ok=False, error="bad_result")
        diagnostic = getattr(transport, "diagnostic", None)
        if isinstance(diagnostic, dict):
            out["transport"] = {key: value for key, value in diagnostic.items()
                                if key in ("operation", "stage", "ok", "status", "error", "raised",
                                           "code", "remote_code")
                                and (type(value) is bool or safe_code(value, None) is not None)}
        out = {key: value for key, value in out.items() if value is not None}
        brainsync._write_private(Path(base) / ".jarvis" / BRAIN_OUTCOME,
                                 json.dumps(out, sort_keys=True).encode("utf-8"), 0o600)
    except Exception:
        pass


def _route(operator, device):
    routes = operator.get("successor_routes") if isinstance(operator, dict) else None
    route = routes.get(device) if isinstance(routes, dict) else None
    if not isinstance(route, dict) or set(route) != {"endpoint", "listen_host", "listen_port", "link"}:
        raise RuntimeRefusal("successor_route_unavailable")
    endpoint = route["endpoint"]
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if (parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or type(route["listen_port"]) is not int
            or not 1 <= route["listen_port"] <= 65535
            or not isinstance(route["listen_host"], str) or not route["listen_host"]):
        raise RuntimeRefusal("successor_route_unavailable")
    try:
        # A tailnet HTTPS reverse proxy may expose 8443 while forwarding to the
        # process's 7781 listener. Keep the two values pinned independently.
        if parsed.port != route["listen_port"]:
            external_only = (parsed.scheme == "https"
                             and parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"})
            if not external_only:
                raise RuntimeRefusal("successor_route_unavailable")
    except ValueError:
        raise RuntimeRefusal("successor_route_unavailable") from None
    if route["listen_host"] not in ("127.0.0.1", "localhost", "::1") and parsed.scheme != "https":
        raise RuntimeRefusal("successor_route_unavailable")
    if route["link"] is not None and not isinstance(route["link"], dict):
        raise RuntimeRefusal("successor_route_unavailable")
    return {"endpoint": endpoint.rstrip("/"), "listen_host": route["listen_host"],
            "listen_port": route["listen_port"], "link": route["link"]}


class ProductionController:
    """Per-process composition rooted in the loaded installation/operator config."""

    def __init__(self, participant, config, base, state, operator_path, operator,
                 *, hub=None, services=None):
        from jarvis.topology import identity
        from .lifecycle import RoleLifecycle
        from .integration import production_issuer

        self.participant, self.config = participant, config
        self.base, self.state, self.operator_path = Path(base), Path(state), Path(operator_path)
        self.operator, self.pins, self.hub, self.services = operator, _pins(operator), hub, services
        self.local = identity(config)[0]
        if self.pins.get(self.local, {}).get("installation_id") != participant.client.installation_id:
            raise RuntimeRefusal("enrollment_pins_missing")
        self.port = getattr(participant.runtime, "_port", None)
        if self.port is None:
            raise RuntimeRefusal("managed_unbound")
        self._dispatch = None
        self._remotes = {}
        self.role_lifecycle = RoleLifecycle(
            participant, self.base / "jarvis.config.json", self.state,
            self.pins, startup_ready=lambda: self.startup_ready())
        self.broker = None if hub is None else hub.broker
        self.issuer = None
        private_key = operator.get("private_key_path")
        if not isinstance(private_key, str) or not private_key:
            raise RuntimeRefusal("owner_proof_unavailable")
        private_key_path = Path(private_key).expanduser()
        if not private_key_path.is_absolute():
            private_key_path = self.operator_path.resolve().parent / private_key_path
        wall = getattr(participant.client, "_wall", None)
        self._production_issuer = production_issuer(
            str(private_key_path), participant.client, **({"wall": wall} if callable(wall) else {}))
        participant.production_controller = self
        participant.repoint_issuer = self.issuer
        participant.repoint_routes = operator.get("successor_routes", {})
        if hub is not None:
            self._attach_hub(hub)

    def startup_ready(self):
        from .startup import startup_ready
        return startup_ready(self.state)

    def __call__(self, old, new, config, base):
        return self._handoff(old, new, config, base)

    def authority_status(self):
        """The role coordinator requires authenticated status on its callback."""
        return self.port.status()

    def candidate_readiness(self, device):
        if device not in self.pins or device == self.local:
            raise RuntimeRefusal("candidacy_mismatch")
        _route(self.operator, device)
        # Validate the same small report rather than trusting truthy peer flags.
        facts = self._remote(device).readiness()
        if (not isinstance(facts, dict)
                or type(facts.get("control_ready")) is not bool
                or type(facts.get("ai_ready")) is not bool):
            raise RuntimeRefusal("bad_readiness")
        return facts

    def _require_owner(self):
        client = self.participant.client
        path = getattr(client, "_owner_key", None)
        if not getattr(client, "can_sign_owner", False) or path is None or not Path(path).is_file():
            raise RuntimeRefusal("owner_proof_unavailable")

    def _remote(self, device):
        if self._dispatch is None:
            raise RuntimeRefusal("managed_unbound")
        remote = self._remotes.get(device)
        if remote is None:
            remote = RemoteRuntime(device, self.port, self._dispatch)
            remote._pins = self.pins
            self._remotes[device] = remote
        return remote

    def _signed_canary(self, device):
        from .participant import SignedCanary
        pin = self.pins.get(device)
        if pin is None:
            return False
        # The shared verifier binds the nonce AND the candidate's exact lease
        # digest. dispatch handles this PC locally without a broker node entry.
        return SignedCanary(self._remote(device), pin["public_key"], pin["installation_id"])(device)

    def canary_local(self):
        """A local MAIN canary needs the actual Hub listener and live guard/runtime."""
        import socket
        hub = self.hub
        if (hub is None or getattr(hub, "started", False) is not True
                or getattr(self.participant.runtime, "_started", False) is not True):
            return False
        try:
            if self.participant.runtime.readiness("control").get("meets") is not True:
                return False
            local = self.participant.guard.local_view()
            if local.get("quarantined") is True or local.get("journal") is not True:
                return False
            host, port = hub.server.server_address[:2]
            probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else "::1" if host == "::" else host
            with socket.create_connection((probe_host, int(port)), timeout=0.5):
                return True
        except Exception:
            return False

    def _brain(self, old, new, config, base):
        from jarvis import brainsync
        from .brain_channel import TransitionBundleTransport
        config = brainsync.jarvis_handoff_config(config)
        transport = None
        result = None
        try:
            transport = TransitionBundleTransport(config, base, self._remote)
            result = brainsync.handoff(old, new, config, base, transport=transport)
            return result
        except Exception as exc:
            result = exc
            raise
        finally:
            _record_brain_outcome(base, old, new, result, transport)

    def _repoint(self, old, new, _lease):
        remote = self._remote(new)
        instruction = remote._request("repoint", target=old)
        return self.role_lifecycle.switch_node(instruction)

    def _handoff(self, old, new, config, base):
        try:
            # Both HTTP owner requests and direct in-process callers pass the
            # same release gate. A node without the owner key may only use the
            # explicitly configured, tuple-scoped proxy to its owner PC.
            self._require_transfer_parity(new)
            route = _route(self.operator, new)
            del route  # candidate process independently consumes its own pinned route.
            owner_port = self._handoff_port(old, new)
            candidate = self._remote(new)
            # The candidate is an executor too. Omitting its fence can keep the
            # handoff waiting for expiry and leaves its prior epoch unfenced.
            peers = [self._remote(device) for device, pin in self.pins.items()
                     if pin.get("kind") == "pc"]
            port = CandidatePort(owner_port, candidate, peers)
            handoff = SafeHandoff(
                port, source_runtime=self.participant.runtime, candidate_runtime=candidate,
                readiness=lambda device: candidate.readiness() if device == new else None,
                repoint=self._repoint, canary=self._signed_canary,
                journal_path=self.state / "continuity-handoff.json", brain=self._brain)
            return handoff(old, new, config, base)
        except RuntimeRefusal as exc:
            return {"ok": False, "status": "refused", "code": exc.code,
                    "main": old, "action": None}

    def _require_transfer_parity(self, device):
        from jarvis.release import code_parity
        from jarvis.release_runtime import require_loaded_code
        try:
            require_loaded_code(self.hub)
        except ValueError:
            raise RuntimeRefusal("code_mismatch") from None
        row = next((item for item in self.hub.broker.nodes() if item.get("id") == device), None)
        parity = code_parity(self.state, reported=row.get("code") if row else None)
        if parity.get("matched") is not True:
            raise RuntimeRefusal("code_mismatch")

    def _handoff_port(self, old, new):
        from .runtime import GatewayPort
        try:
            self._require_owner()
            return GatewayPort(self.participant.client)
        except RuntimeRefusal as exc:
            if exc.code != "owner_proof_unavailable":
                raise
        target = self.operator.get("owner_proxy_device")
        if (target != new or old != self.local or self.operator.get("device_id") != self.local
                or target not in self.pins or self.local not in self.pins):
            raise RuntimeRefusal("owner_proof_unavailable")
        remote = self._remote(target)
        return _OwnerProxyPort(self, remote, self.local, new)

    def _attach_hub(self, hub):
        from .repoint import Issuer as RepointIssuer
        self.hub = hub
        self.broker = hub.broker
        self.role_lifecycle.after_response = hub.defer_after_response
        self.issuer = RepointIssuer(self.participant, self.state, self.broker)
        self._dispatch = lambda node, payload: __import__(
            "jarvis.continuity.participant", fromlist=["dispatch"]).dispatch(hub, node, payload)
        # The installation key and enrolled public pins stay on each PC. The
        # authority owner key is not copied to a successor: owner operations
        # remain explicitly unavailable there until provisioned separately.
        self.recovery = self._recovery()
        hub.continuity_recovery = self._recover
        hub.continuity_handoff = self
        from .integration import configure_production
        configure_production(self.state, self.participant.runtime,
            executor=self.participant.guard, handoff=self,
            issuer=self._production_issuer)

    def attach_hub(self, hub):
        self._attach_hub(hub)

    def _recovery(self):
        from .recovery import ManualRecovery
        candidate = self._remote(self.local)
        peers = [self._remote(device) for device, pin in self.pins.items()
                 if pin.get("kind") == "pc"]
        port = CandidatePort(self.port, candidate, peers)
        return ManualRecovery(port, candidate, lambda _device: self._signed_canary(self.local),
                              receipts=self.state / "continuity-recovery-receipts.json")

    def _recover(self, device, request_id):
        self._require_owner()
        if self.recovery is None:
            raise RuntimeRefusal("managed_unbound")
        return self.recovery(device, request_id)

    def prepare_repoint(self, target):
        """Candidate-side issue, from its own operator route and installed key only."""
        if not isinstance(target, str) or target == self.local:
            raise RuntimeRefusal("candidacy_mismatch")
        # This PC is the candidate and will serve the endpoint receipt itself.
        # Use its own pinned route; the target route belongs to the old MAIN.
        route = _route(self.operator, self.local)
        if target not in self.pins or target == self.local:
            raise RuntimeRefusal("candidacy_mismatch")
        hub = self._ensure_standby_hub(route)
        if not getattr(hub, "started", False):
            raise RuntimeRefusal("successor_hub_unavailable")
        # A return approval is an explicit operator-prepared receipt. Consume
        # it only after the return bundle has been received and the authority
        # has granted this exact not-ready tuple; ordinary handoffs have none.
        approval = self.state / "brainsync-return-approval.json"
        if approval.exists() or approval.is_symlink():
            from .return_brain import activate
            activate(self.participant)
        from .repoint import Issuer as RepointIssuer
        self.issuer = RepointIssuer(self.participant, self.state, hub.broker)
        return self.issuer.issue(target, route["endpoint"], link=route["link"])

    def ensure_local_main_hub(self):
        """Recovery on a node must stand up its own bound Hub before canarying."""
        if (self.hub is not None and getattr(self.hub, "started", False) is True
                and getattr(self.hub, "main", None) == self.local):
            return self.hub
        return self._ensure_standby_hub(_route(self.operator, self.local))

    def _ensure_standby_hub(self, route):
        hub = self.hub
        if hub is not None and getattr(hub, "started", False):
            if (hub.main != self.local or getattr(hub, "continuity_endpoint", None) != route["endpoint"]):
                raise RuntimeRefusal("successor_hub_unavailable")
            return hub
        from jarvis.cluster import Hub
        from .startup import acting_main_config
        view = acting_main_config(self.config)
        cluster = view.setdefault("cluster", {})
        # The transition-scoped endpoint is a control listener only. It must
        # not start an updater/release worker or serve ordinary admission.
        cluster["auto_update"] = False
        cluster["auto_release"] = False
        # Host validation pins the advertised origin. A tailnet Host header is
        # accepted only when it is the same exact route advertised by this Hub.
        cluster["main_url"] = route["endpoint"]
        standby = Hub(view, self.base, self.state)
        standby.continuity_endpoint = route["endpoint"]
        standby.continuity_standby = True
        self.attach_hub(standby)
        try:
            standby.start(host=route["listen_host"], port=route["listen_port"])
        except Exception as exc:
            try:
                standby.close()
            except Exception:
                pass
            self.hub = None
            raise RuntimeRefusal("successor_hub_unavailable") from exc
        self.hub = standby
        self._dispatch = lambda node, payload: __import__(
            "jarvis.continuity.participant", fromlist=["dispatch"]).dispatch(standby, node, payload)
        return standby


    def close(self):
        hub, self.hub = self.hub, None
        if hub is not None and getattr(hub, "continuity_standby", False):
            hub.close()


class _OwnerProxyPort:
    """Delegate only Dell->Zenbook return-transition owner proofs.

    Status and all non-owner calls remain on the local installation client.
    The remote participant validates the live tuple and signs with the owner
    key physically present on the owner PC.
    """
    def __init__(self, controller, remote, source, candidate):
        from .runtime import GatewayPort
        self.controller, self.remote = controller, remote
        self.source, self.candidate = source, candidate
        self.local = GatewayPort(controller.participant.client)

    def __getattr__(self, name):
        return getattr(self.local, name)

    def transition_request(self, request):
        if (not isinstance(request, dict)
                or set(request) != {"caller", "client_key", "action", "candidate",
                                    "expected_incarnation", "expected_epoch", "expected_policy", "profile"}
                or request.get("caller") != "safe-handoff"
                or request.get("action") != "set_preferred_main"
                or request.get("candidate") != self.candidate
                or request.get("profile") != "control"):
            raise RuntimeRefusal("forbidden")
        status = self.local.status()
        if (status.get("main") != self.source or status.get("lease_valid") is not True
                or status.get("main_ready") is not True or status.get("mode") != "V2_ACTIVE"
                or status.get("transition") or any(request.get(k) != status.get(sk) for k, sk in (
                    ("expected_incarnation", "incarnation"), ("expected_epoch", "epoch"),
                    ("expected_policy", "policy_version")))):
            raise RuntimeRefusal("stale")
        from .runtime import _caller_key
        if request.get("client_key") != _caller_key(
                "set_preferred_main", self.source, self.candidate, status):
            raise RuntimeRefusal("forbidden")
        return self.remote._request("owner_proxy", operation={"kind": "request"})

    def transition_step(self, transition_id, step, canary_ok=None):
        if step not in {"drain", "fence", "grant", "complete", "cancel"}:
            raise RuntimeRefusal("forbidden")
        if step == "complete" and type(canary_ok) is not bool:
            raise RuntimeRefusal("bad_request")
        if step != "complete" and canary_ok is not None:
            raise RuntimeRefusal("bad_request")
        status = self.local.status()
        transition = status.get("transition") or {}
        if (transition.get("transition_id") != transition_id
                or transition.get("action") != "set_preferred_main"
                or transition.get("source") != self.source
                or transition.get("candidate") != self.candidate):
            raise RuntimeRefusal("stale")
        operation = {"kind": "step", "transition_id": transition_id,
                     "step": step, "incarnation": status["incarnation"], "epoch": status["epoch"]}
        if step == "complete":
            operation["canary_ok"] = canary_ok
        return self.remote._request("owner_proxy", operation=operation)

def build_controller(participant, config, base, state, operator_path, operator,
                     *, hub=None, services=None):
    """Build all production adapters from one bound local participant."""
    return ProductionController(participant, config, base, state, operator_path, operator,
                                hub=hub, services=services)
