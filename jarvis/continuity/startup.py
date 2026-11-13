"""Bind continuity at startup, and run MAIN's services on a node the authority promoted.

Inert by default. Nothing here runs unless this state directory holds the
continuity-managed marker (`integration.continuity_state(state) == "managed"`) and
`jarvis.config.json` names `continuity.operator_config`. Then startup:

1. loads this PC's operator file (device key path, gateway origin) via
   `participant.load_local`, with a real local readiness probe;
2. records the production binding (runtime + admission issuer) and registers the
   participant, so `renewal.start_bound` starts the lease renewer and the gateway's
   self-recovery on this PC;
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
        _log(f"MAIN services stopped ({reason})")
        return True

    def sync(self, runtime):
        if not self.running():
            return
        try:
            runtime.require_authority("control")
        except Exception as exc:
            self.deactivate(getattr(exc, "code", None) or type(exc).__name__)


def _close(thing):
    for name in ("close", "shutdown"):
        fn = getattr(thing, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            return


def bind_managed(config, state, base=None, *, client_factory=None, services=None, boot=None):
    """Bind this PC's continuity participant for `state`, or return None when unmanaged.

    Never raises: a broken enrollment is logged and the PC starts as before (every
    effect still checks `require_production_authority`, which refuses while managed
    and unbound).
    """
    from .integration import (IntegrationError, configure_production, continuity_state,
                              production_issuer)
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
            return existing
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
            participant = load_local(config, state, probe=local_probe(state, services),
                                     client_factory=remember, boot=boot or boot_id)
            key = (captured.get("data") or {}).get("private_key_path")
            if not key:
                raise IntegrationError("bad_request")
            key_path = Path(key).expanduser()
            if not key_path.is_absolute():
                key_path = captured["path"].resolve().parent / key_path
            configure_production(state, participant.runtime, executor=participant.guard,
                                 issuer=production_issuer(str(key_path), participant.client))
            if services is not None:
                participant.canary = services.activate
                participant.services = services
            bind(state, participant)
            # The first status read is a network call: keep it off the startup path.
            threading.Thread(target=participant.runtime.start, name="continuity-runtime-start",
                             daemon=True).start()
            _log(f"bound as {participant.runtime.device_id}")
            return participant
        except Exception as exc:
            _log(f"binding failed ({type(exc).__name__}: {exc}); starting unbound")
            return None
