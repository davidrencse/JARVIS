"""Local Windows cutover using the existing cooperative NodeWorker lifecycle.

The worker's nonce-bound request/ack is implemented by ``jarvis.cluster``. This
module never signals or kills a process. A stop is considered complete only
after that exact receipt exists *and* the worker releases ``cluster-node.lock``.
"""
import time
from pathlib import Path


SERVICE_NAME = "jarvis-cluster-node"
NODE_LOCK = "cluster-node.lock"


class CooperativeStopError(RuntimeError):
    pass


def _registration(state):
    from jarvis.cluster import _PROCESS_RECORD, _local_record
    return _local_record(Path(state) / _PROCESS_RECORD)


def _lock_is_busy(state):
    from jarvis.locking import exclusive_lock
    try:
        with exclusive_lock(Path(state) / NODE_LOCK):
            return False
    except ValueError:
        return True


def _worker_is_active(state, config_path, device_id):
    from jarvis.cluster import _PROCESS_RECORD, _local_pid_alive, _local_record
    import json
    state = Path(state).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    record = _local_record(state / _PROCESS_RECORD)
    try:
        from jarvis.topology import identity
        configured_device = identity(json.loads(config.read_text(encoding="utf-8")))[0]
    except Exception:
        return False
    return bool(isinstance(record, dict)
                and set(record) == {"schema", "kind", "pid", "nonce", "config", "state"}
                and record.get("schema") == 1 and record.get("kind") == "node-worker"
                and record.get("config") == str(config)
                and record.get("state") == str(state)
                and configured_device == device_id
                and type(record.get("pid")) is int and _local_pid_alive(record["pid"])
                and isinstance(record.get("nonce"), str) and len(record["nonce"]) == 48
                and all(c in "0123456789abcdef" for c in record["nonce"])
                and _lock_is_busy(state))


def request_stop(state, config_path, device_id, *, timeout=40.0, poll=0.05,
                 clock=time.monotonic, sleep=time.sleep):
    """Stop this exact local worker and prove its writer lock is released."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 45:
        raise CooperativeStopError("timeout_invalid")
    from jarvis.cluster import _PROCESS_RECORD, _STOP_ACK, request_node_worker_stop
    state = Path(state).expanduser().resolve(strict=True)
    config = Path(config_path).expanduser().resolve(strict=True)
    before = _registration(state)
    if not _worker_is_active(state, config, device_id):
        raise CooperativeStopError("worker_registration_unverified")
    try:
        receipt = request_node_worker_stop(config, state, timeout=float(timeout))
    except Exception as exc:
        # Do not expose config or state paths from low-level errors.
        code = "worker_stop_timeout" if isinstance(exc, TimeoutError) else "worker_stop_refused"
        raise CooperativeStopError(code) from None
    deadline = clock() + float(timeout)
    while clock() < deadline:
        current = _registration(state)
        ack = _read_ack(state / _STOP_ACK)
        if (current is None and ack is not None
                and all(ack.get(k) == before.get(k) for k in
                        ("schema", "kind", "pid", "nonce", "config", "state"))
                and ack.get("request_id") == receipt.get("request_id")
                and ack.get("status") == "stopped"):
            if not _lock_is_busy(state):
                return {"stopped": True, "request_id": receipt["request_id"],
                        "registration": before}
        sleep(min(float(poll), max(0.0, deadline - clock())))
    raise CooperativeStopError("worker_close_unverified")


def _read_ack(path):
    from jarvis.cluster import _local_record
    return _local_record(path)


def wait_for_registration(state, config_path, device_id, *, previous_nonce=None,
                          timeout=40.0, poll=0.05, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + float(timeout)
    while clock() < deadline:
        record = _registration(state)
        if (_worker_is_active(state, config_path, device_id)
                and isinstance(record, dict) and record.get("nonce") != previous_nonce):
            return record
        sleep(min(float(poll), max(0.0, deadline - clock())))
    raise CooperativeStopError("worker_restore_unverified")


class WindowsNodeLifecycle:
    """Pause and restore the existing NodeWorker through its current launch helper."""
    service_name = SERVICE_NAME

    def __init__(self, config_path, state, device_id, *, timeout=40.0, launch=None,
                 windows=None):
        self.config_path = Path(config_path).expanduser().resolve()
        self.state = Path(state).expanduser().resolve()
        self.device_id, self.timeout = device_id, timeout
        self._launch = launch
        import os
        self._windows = (os.name == "nt") if windows is None else windows
        self._attempted = False
        self._stopped = None
        self._old_registration = None

    def supported(self):
        return self._windows is True

    def active(self, _service):
        return _worker_is_active(self.state, self.config_path, self.device_id)

    def pause(self, _state):
        self._attempted = True
        self._old_registration = _registration(self.state)
        try:
            self._stopped = request_stop(self.state, self.config_path, self.device_id,
                                         timeout=self.timeout)
        except CooperativeStopError:
            raise
        return {"paused": True, "stopped": [self.service_name], "failed": [],
                "request_id": self._stopped["request_id"]}

    def wake(self, _state):
        if not self._attempted:
            return {"woke": False, "failed": []}
        try:
            previous = ((self._stopped or {}).get("registration") or
                        self._old_registration or {}).get("nonce")
            # A timed-out request may still be pending. Do not launch beside a live writer.
            if self.active(self.service_name):
                return {"woke": True, "already_running": True, "failed": []}
            if _lock_is_busy(self.state):
                deadline = time.monotonic() + self.timeout
                while _lock_is_busy(self.state) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _lock_is_busy(self.state):
                    raise CooperativeStopError("worker_restore_unverified")
            launch = self._launch
            if launch is None:
                from jarvis.cluster_setup import _launch_agent
                launch = _launch_agent
            launch(self.config_path, self.state)
            wait_for_registration(self.state, self.config_path, self.device_id,
                                  previous_nonce=previous, timeout=self.timeout)
            return {"woke": True, "failed": []}
        except Exception:
            raise CooperativeStopError("worker_restore_unverified") from None
