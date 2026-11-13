"""Bind continuity at startup, and run MAIN's services on a node the authority promoted.

Inert by default. Nothing here runs unless this state directory holds the
continuity-managed marker (`integration.continuity_state(state) == "managed"`) and
`jarvis.config.json` names `continuity.operator_config`. Then startup:

1. loads this PC's operator file (device key path, gateway origin) via
   `participant.load_local`, with a real local readiness probe;
2. records the production binding (runtime, executor guard, admission issuer) and
   registers the participant, so `renewal.start_bound` starts the lease renewer and
   the gateway's self-recovery on this PC. A config with no enrollment keys does not
   bind. A failed grant or a failed register leaves no runtime behind. The authority
   owner private key is not copied to a successor; owner-only recovery and handoff
   requests fail with `owner_proof_unavailable` there until a supported owner signer
   is provisioned;
3. on a node (not the configured main), wires `NodeMainServices` as the participant's
   canary: when the authority grants this node MAIN, it starts a local console and the
   phone server from the node's own state (a degraded start: no pull from the old MAIN,
   which may be dead), and stops them again as soon as the lease is gone.

The configured main keeps its usual console and phone server; there the canary only
checks that the process is up.
"""
import copy
import hashlib
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _log(message):
    print(f"JARVIS continuity: {message}", flush=True)


def acting_main_config(config):
    """An in-memory view of `config` with this PC as the cluster main. Never written."""
    from jarvis.topology import identity
    view = copy.deepcopy(config or {})
    me = identity(view)[0]
    view.setdefault("cluster", {})["main_device"] = me
    return view


def checkpoint_id(state):
    """A short id for the brain state this PC would start from.

    The successor never pulls from the old MAIN. It starts from its own memory
    database (kept current by the vault/brainsync paths that already exist), so the
    checkpoint names that local copy: size and modification time of memory.sqlite3.
    """
    path = Path(state) / "memory.sqlite3"
    try:
        info = path.stat()
        seed = f"{info.st_size}:{info.st_mtime_ns}"
    except OSError:
        seed = "empty"
    return "cp-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def local_probe(state, services=None):
    """Readiness facts for this PC. Control is ready while the process runs; on a node
    whose MAIN services failed to start, it is not."""
    def probe():
        ready = services is None or services.healthy()
        return {"control_ready": ready, "checkpoint": checkpoint_id(state)}
    return probe


class NodeMainServices:
    """Console + phone server on a node while it holds the MAIN lease.

    `activate()` is the canary the self-recovery step calls before completing the grant:
    it returns True only when the phone server is actually listening. `sync(runtime)`
    runs on every renewer tick and stops the services once the runtime no longer holds
    authority, so a demoted node stops answering the phone at once.
    """

    def __init__(self, config, base, state, *, console_factory=None, phone_start=None):
        self.config, self.base, self.state = config, base, state
        self._console_factory = console_factory
        self._phone_start = phone_start
        self.console = self.phone = None
        self._failed = False
        self._lock = threading.Lock()

    def healthy(self):
        return not self._failed

    def running(self):
        return self.phone is not None

    def activate(self):
        with self._lock:
            if self.phone is not None:
                return True
            view = acting_main_config(self.config)
            try:
                if self._console_factory is None:
                    from jarvis.ui import Console
                    # warm=False: no daily consolidation or brain warm-up on a stand-in MAIN.
                    console = Console(view, self.base, self.state).start_live(warm=False)
                else:
                    console = self._console_factory(view, self.base, self.state)
                if self._phone_start is None:
                    from jarvis import phone
                    server = phone.start(view, self.state, console=console, hub=None)
                else:
                    server = self._phone_start(view, self.state, console)
            except Exception as exc:
                self._failed = True
                _log(f"MAIN services did not start ({type(exc).__name__}: {exc})")
                return False
            if server is None:
                _close(console)
                self._failed = True
                _log("MAIN services did not start (phone server off or port busy)")
                return False
            self.console, self.phone, self._failed = console, server, False
            _log("this PC is MAIN now: console and phone server started")
            return True

    def deactivate(self, reason="lease gone"):
        with self._lock:
            phone, console = self.phone, self.console
            self.phone = self.console = None
        if phone is None and console is None:
            return False
        _close(phone)
        _close(console)
        try:
            from .participant import lookup
            participant = lookup(self.state)
            controller = getattr(participant, "production_controller", None)
            if controller is not None:
                controller.close()
        except Exception:
            pass
        _log(f"MAIN services stopped ({reason})")
        return True

    def sync(self, runtime):
        # HANDOVER intentionally denies ordinary admission. Calling
        # require_authority there drops the candidate's in-memory grant and
        # destroys its standby listener while the coordinator is canarying it.
        # Keep only the already-accepted, authenticated not-ready candidacy;
        # this does not start services or permit any ordinary work.
        if self._preparing(runtime):
            return
        try:
            runtime.require_authority("control")
        except Exception as exc:
            reason = getattr(exc, "code", None) or type(exc).__name__
            if self.running():
                try:
                    from .participant import lookup
                    participant = lookup(self.state)
                    controller = getattr(participant, "production_controller", None)
                    hub = getattr(controller, "hub", None)
                    defer = getattr(hub, "defer_after_response", None)
                    if callable(defer) and defer(lambda: self.deactivate(reason)):
                        return
                except Exception:
                    pass
                self.deactivate(reason)
            else:
                try:
                    from .participant import lookup
                    participant = lookup(self.state)
                    controller = getattr(participant, "production_controller", None)
                    if controller is not None:
                        controller.close()
                except Exception:
                    pass

    def _preparing(self, runtime):
        from .participant import lookup
        participant = lookup(self.state)
        if participant is None or participant.runtime is not runtime:
            return False
        snapshot = runtime.lease_snapshot()
        if not snapshot:
            return False
        try:
            status = participant.client.status()
        except Exception:
            return False
        transition = status.get("transition") or {}
        return (status.get("mode") == "HANDOVER"
                and status.get("lease_valid") is True
                and status.get("main_ready") is False
                and status.get("main") == participant.device_id
                and snapshot.get("holder") == participant.device_id
                and snapshot.get("incarnation") == status.get("incarnation")
                and snapshot.get("epoch") == status.get("epoch")
                and transition.get("candidate") == participant.device_id
                and transition.get("state") == "GRANTED_NOT_READY")


def _close(thing):
    for name in ("close", "shutdown"):
        fn = getattr(thing, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            return


def startup_ready(state):
    """True when this process holds this PC's runtime, guard, and issuer together.

    A marker with no binding, or a runtime without the guard that fences local
    effects, is not ready. Role lifecycle stays unavailable until this is true.
    """
    from .integration import production_binding
    from .participant import lookup
    participant = lookup(state)
    binding = production_binding(state)
    guard = None if participant is None else getattr(participant, "guard", None)
    if not isinstance(binding, dict) or participant is None or guard is None:
        return False
    return (binding.get("runtime") is participant.runtime
            and binding.get("executor") is guard
            and callable(getattr(guard, "accept", None))
            and callable(binding.get("issuer")))


def _enrollment_keys(data):
    """Device id to public key, from an operator enrollment map. None when incomplete."""
    raw = (data or {}).get("enrollment") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        return None
    keys = {}
    for device, record in raw.items():
        if not isinstance(device, str) or not device:
            return None
        if isinstance(record, str):
            key = record
        elif isinstance(record, dict):
            key = record.get("public_key")
        else:
            return None
        if not isinstance(key, str) or not key.strip():
            return None
        keys[device] = key
    return keys


def _grant_pins(data):
    raw = (data or {}).get("enrollment") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    pins = {}
    for device, record in raw.items():
        if not isinstance(record, dict):
            continue
        key = record.get("public_key")
        installation = record.get("installation_id")
        if isinstance(device, str) and isinstance(key, str) and isinstance(installation, str):
            pins[device] = {"installation_id": installation, "public_key": key}
    return pins or None


def _open_guard(state, participant, data, boot):
    """Real verifier and executor journal. None when the operator file cannot build them."""
    keys = _enrollment_keys(data)
    fleet_id = (data or {}).get("fleet_id") if isinstance(data, dict) else None
    if keys is None or not isinstance(fleet_id, str) or not fleet_id:
        return None
    from .executor import ExecutorGuard
    from .integration import IntegrationError, SignedAuthority
    client = participant.client
    authority = SignedAuthority(client, keys)
    kwargs = {}
    wall = getattr(client, "_wall", None)
    if callable(wall):
        kwargs["wall"] = wall
    guard = ExecutorGuard(
        Path(state) / "continuity-executor.sqlite",
        device_id=client.device_id, installation_id=client.installation_id,
        fleet_id=fleet_id, authority_id=client.authority_id,
        authority=authority, verifier=authority.verify, boot=boot, **kwargs)
    try:
        if guard.local_view().get("quarantined"):
            raise IntegrationError("quarantined")
    except Exception:
        guard.close()
        raise
    return guard


def _release_failed_bind(state, guard):
    from .integration import clear_production
    from .participant import unbind
    try:
        clear_production(state)
    except Exception:
        pass
    try:
        unbind(state)
    except Exception:
        pass
    if guard is not None:
        try:
            guard.close()
        except Exception:
            pass


def _attach_controller(participant, config, state, base, operator_path, operator, *, hub=None, services=None):
    controller = getattr(participant, "production_controller", None)
    if controller is None:
        from .production import build_controller
        controller = build_controller(participant, config, base or Path(state).resolve().parent,
                                      state, operator_path, operator, hub=hub, services=services)
    elif hub is not None:
        controller.attach_hub(hub)
    participant.production_controller = controller
    participant.role_lifecycle = controller.role_lifecycle
    return controller


def bind_managed(config, state, base=None, *, client_factory=None, services=None, boot=None, hub=None):
    """Bind this PC's continuity participant for `state`, or return None when unmanaged.

    Never raises: a broken enrollment is logged and the PC starts as before (every
    effect still checks `require_production_authority`, which refuses while managed
    and unbound). The guard and issuer are recorded together. A missing enrollment,
    a bad successor grant, or a failure after that point leaves no runtime bound.
    """
    from .integration import (IntegrationError, configure_production, continuity_state)
    from .participant import bind, load_local, lookup
    try:
        if continuity_state(state) != "managed":
            return None
    except IntegrationError as exc:
        _log(f"continuity marker unusable ({exc}); not binding")
        return None
    raw = ((config or {}).get("continuity") or {}).get("operator_config")
    if not isinstance(raw, str) or not raw:
        _log("managed, but continuity.operator_config is not set; not binding")
        return None
    with _LOCK:
        existing = lookup(state)
        if existing is not None:
            operator_path = getattr(existing, "operator_path", None)
            operator = getattr(existing, "operator_data", None)
            if operator_path is not None and isinstance(operator, dict):
                _attach_controller(existing, config, state, base, operator_path, operator,
                                   hub=hub, services=services or getattr(existing, "services", None))
            return existing
        guard = None
        committed = False
        try:
            from jarvis.topology import is_node
            if services is None and is_node(config):
                services = NodeMainServices(config, base or Path(state).resolve().parent, state)
            factory = client_factory
            if factory is None:
                from .cli import build_client
                factory = build_client
            captured = {}

            def remember(path):
                client, data = factory(path)
                captured["path"], captured["data"] = Path(path), data
                return client, data

            from .clock import boot_id
            boot_fn = boot or boot_id
            participant = load_local(config, state, probe=local_probe(state, services),
                                     client_factory=remember, boot=boot_fn)
            operator = captured.get("data") or {}
            operator_path = captured.get("path")
            key = operator.get("private_key_path")
            if not key:
                raise IntegrationError("bad_request")
            key_path = Path(key).expanduser()
            if not key_path.is_absolute():
                key_path = captured["path"].resolve().parent / key_path
            guard = _open_guard(state, participant, operator, boot_fn)
            if guard is None:
                _log("managed, but the operator config has no fleet_id or enrollment keys; not binding")
                return None
            participant.guard = guard
            # A successor process consumes its one grant before ordinary effects are armed.
            from .lifecycle import GRANT_FILE, accept_successor_grant
            if (Path(state) / GRANT_FILE).exists():
                pins = _grant_pins(operator)
                if not pins:
                    raise IntegrationError("managed_unbound")
                accept_successor_grant(participant, state, pins)
            participant.operator_path = operator_path
            participant.operator_data = operator
            participant.base = Path(base or Path(state).resolve().parent)
            participant.services = services
            controller = _attach_controller(participant, config, state, base, operator_path,
                                             operator, hub=hub, services=services)
            configure_production(state, participant.runtime, executor=guard,
                                 handoff=controller if hub is not None else None,
                                 issuer=controller._production_issuer)
            if services is not None:
                def canary():
                    try:
                        hub = controller.ensure_local_main_hub()
                    except Exception:
                        return False
                    return (hub.started is True and services.activate() is True)
                participant.canary = canary
            else:
                participant.canary = controller.canary_local
            bind(state, participant)
            # The first status read is a network call: keep it off the startup path.
            threading.Thread(target=participant.runtime.start, name="continuity-runtime-start",
                             daemon=True).start()
            committed = True
            _log(f"bound as {participant.runtime.device_id}")
            return participant
        except Exception as exc:
            _log(f"binding failed ({type(exc).__name__}: {exc}); starting unbound")
            return None
        finally:
            if not committed:
                _release_failed_bind(state, guard)
