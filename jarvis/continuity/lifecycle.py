"""Explicit successor role/tunnel activation through the existing unit helpers.

These actions never run at import, on a timer, or merely because a PC is offline.
The old worker must have acknowledged its result and released its role locks.
The controller still needs to orchestrate that deferred boundary.
"""
import json
from pathlib import Path

from .runtime import RuntimeRefusal
from .repoint import FILE, _read, _save, _grant, verify, Installer

GRANT_FILE = 'continuity-successor-grant.json'


class RoleLifecycle:
    def __init__(self, participant, config_path, state, pins, *, probe=None, startup_ready=None):
        self.participant, self.config_path, self.state, self.pins = participant, Path(config_path), Path(state), pins
        self.startup_ready = startup_ready
        self.installer = Installer(participant, state, pins, **({'probe': probe} if probe is not None else {}))

    def _body(self, instruction):
        if not callable(self.startup_ready) or self.startup_ready() is not True:
            raise RuntimeRefusal("startup_incomplete")
        p = self.participant
        body = verify(instruction, authority_id=p.client.authority_id, target=p.device_id, pins=self.pins)
        _grant(p.client.status(), body)
        from .integration import production_binding
        binding = production_binding(self.state)
        if not binding or binding.get('runtime') is not p.runtime or binding.get('executor') is not p.guard:
            raise RuntimeRefusal('managed_unbound')
        fence = p.guard.local_view()
        if (fence.get('quarantined') or fence.get('fenced_incarnation') != body['incarnation'] or
                fence.get('fenced_epoch', -1) != body['epoch']-1 or not fence.get('legacy_closed')):
            raise RuntimeRefusal('target_unfenced')
        return body

    def prepare_successor(self, instruction):
        """Candidate only, after its old node process has stopped cooperatively.

        A grant is kept privately only for this not-ready role handover. This is
        never recovery of a ready lease after a crash or reconstruction from status.
        """
        from jarvis.locking import exclusive_lock
        from jarvis import cluster_tunnel, cluster_setup
        body = self._body(instruction)
        if body['main'] != self.participant.device_id:
            raise RuntimeRefusal('not_candidate')
        lease = self.participant.runtime.lease_snapshot()
        if not lease or any(lease.get(k) != body[k] for k in ('incarnation', 'epoch')):
            raise RuntimeRefusal('lease_unavailable')
        with exclusive_lock(self.state/'cluster-node.lock'), exclusive_lock(self.state/'cluster-main.lock'):
            _grant(self.participant.client.status(), body)
            # A forward tunnel owns the candidate's hub port. It must have let go
            # before the successor unit starts. A refused stop never starts a hub.
            if cluster_tunnel.status(self.state)['running']:
                cluster_tunnel.stop(self.state)
                if cluster_tunnel.status(self.state)['running']:
                    raise RuntimeRefusal('tunnel_busy')
            _save(self.state/GRANT_FILE, {'instruction': instruction, 'lease': lease})
            _save(self.state/FILE, instruction)
            _save(self.state/'cluster-node.json', {'node_id': body['target'], 'main_device': body['main'],
                'token': body['token'], 'code_key': body['code_key']})
            self.participant.guard.close()  # successor owns the same executor writer lock
        # Explicit role action; tests patch this helper, never a real manager.
        report = cluster_setup.role_autostart(self.config_path, self.state, True, restart=True)
        from jarvis.autostart import system
        if system() == 'windows':
            cluster_setup._launch_agent(self.config_path, self.state)
        return {'scheduled': True, 'autostart': report, 'main': body['main']}

    def switch_node(self, instruction):
        """Publish only after the new endpoint proves its signing key and nonce."""
        from jarvis import cluster_tunnel, cluster_setup
        body = self._body(instruction)
        if body['main'] == self.participant.device_id:
            raise RuntimeRefusal('not_candidate')
        result = self.installer.install(instruction)
        # Signed link None means direct pinned HTTPS, not a guessed SSH host.
        if cluster_tunnel.status(self.state)['running']:
            cluster_tunnel.stop(self.state)
            if cluster_tunnel.status(self.state)['running']:
                raise RuntimeRefusal('tunnel_busy')
        from jarvis.linkfinder import activate_tunnel
        activate_tunnel(self.config_path, self.state, body['link'])
        cluster_setup.role_autostart(self.config_path, self.state, True)
        return result


def accept_successor_grant(participant, state, pins):
    """Consume the real grant once, verifying it with the gateway before accepting."""
    from jarvis.locking import exclusive_lock
    path = Path(state)/GRANT_FILE
    if not path.exists():
        return False
    with exclusive_lock(Path(state)/'continuity-grant.lock'):
        record = _read(path)
        if set(record) != {'instruction', 'lease'}:
            raise RuntimeRefusal('bad_state')
        body = verify(record['instruction'], authority_id=participant.client.authority_id,
                      target=participant.device_id, pins=pins)
        if body['main'] != participant.device_id:
            raise RuntimeRefusal('not_candidate')
        status = participant.client.status(); _grant(status, body)
        lease = record['lease']
        if (not isinstance(lease, dict) or set(lease) != {'holder', 'lease_id', 'incarnation', 'epoch'} or
                lease['holder'] != participant.device_id or
                any(lease[k] != body[k] for k in ('incarnation', 'epoch'))):
            raise RuntimeRefusal('stale')
        # accept_lease alone checks tuple identity; renew proves the grant id was real.
        renewed = participant.client.renew(lease['holder'], lease['lease_id'], lease['incarnation'], lease['epoch'])
        if not isinstance(renewed, dict) or renewed.get('ready') is not False:
            raise RuntimeRefusal('stale')
        _grant(participant.client.status(), body)
        participant.runtime.start()
        participant.runtime.accept_lease(lease)
        path.unlink()  # a later ready-process restart must use manual recovery
        return True
