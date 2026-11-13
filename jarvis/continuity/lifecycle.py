"""Explicit successor role/tunnel activation through the existing unit helpers.

These actions never run at import, on a timer, or merely because a PC is offline.
The old worker must have acknowledged its result and released its role locks.
The controller still needs to orchestrate that deferred boundary.

A failed unit start is retried from the saved phase. The executor journal stays
closed, the grant file is not rewritten, and a tunnel this handover already
started is not stopped just to retry the unit. A stale grant, a busy tunnel,
or a publication that does not match the signed instruction refuses before
that retry starts anything.
"""
import copy
import hashlib
import json
from pathlib import Path

from .runtime import RuntimeRefusal
from .repoint import FILE, _read, _save, _grant, verify, Installer

GRANT_FILE = 'continuity-successor-grant.json'
PHASE_FILE = 'continuity-role-phase.json'
_PHASE_FIELDS = {'action', 'phase', 'incarnation', 'epoch', 'transition_id', 'digest'}
_PREPARE_PHASES = {'intent', 'released', 'scheduled'}
_SWITCH_PHASES = {'intent', 'activating', 'tunnel_ready', 'published', 'scheduled'}


def _digest(instruction):
    raw = json.dumps(instruction, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _note_for(body):
    note = {'node_id': body['target'], 'main_device': body['main'],
            'token': body['token'], 'code_key': body['code_key']}
    if body.get('link') is not None:
        note['link'] = copy.deepcopy(body['link'])
    return note


class RoleLifecycle:
    def __init__(self, participant, config_path, state, pins, *, probe=None, startup_ready=None):
        self.participant, self.config_path, self.state, self.pins = participant, Path(config_path), Path(state), pins
        self.startup_ready = startup_ready
        self.after_response = None
        self.installer = Installer(participant, state, pins, **({'probe': probe} if probe is not None else {}))

    def _require_startup(self):
        if not callable(self.startup_ready) or self.startup_ready() is not True:
            raise RuntimeRefusal('startup_incomplete')

    def _bound(self):
        from .integration import production_binding
        binding = production_binding(self.state)
        p = self.participant
        if not binding or binding.get('runtime') is not p.runtime or binding.get('executor') is not p.guard:
            raise RuntimeRefusal('managed_unbound')

    def _body(self, instruction):
        self._require_startup()
        p = self.participant
        body = verify(instruction, authority_id=p.client.authority_id, target=p.device_id, pins=self.pins)
        _grant(p.client.status(), body)
        self._bound()
        fence = p.guard.local_view()
        if (fence.get('quarantined') or fence.get('fenced_incarnation') != body['incarnation'] or
                fence.get('fenced_epoch', -1) != body['epoch']-1 or not fence.get('legacy_closed')):
            raise RuntimeRefusal('target_unfenced')
        return body

    def _require_lease(self, body):
        lease = self.participant.runtime.lease_snapshot()
        if not lease or any(lease.get(k) != body[k] for k in ('incarnation', 'epoch')):
            raise RuntimeRefusal('lease_unavailable')
        return lease

    def _mark(self, action, phase, body, instruction):
        _save(self.state/PHASE_FILE, {
            'action': action, 'phase': phase, 'incarnation': body['incarnation'], 'epoch': body['epoch'],
            'transition_id': body['transition_id'], 'digest': _digest(instruction)})

    def _same_phase(self, action, body, instruction):
        """Matching handover phase, or None when an older phase may be replaced.

        A newer saved tuple is stale. The same tuple with a different action,
        transition, or instruction is a conflict. The file is metadata only.
        """
        path = self.state/PHASE_FILE
        if not path.exists():
            return None
        record = _read(path)
        if (set(record) != _PHASE_FIELDS or record.get('action') not in ('prepare', 'switch') or
                type(record.get('incarnation')) is not int or type(record.get('epoch')) is not int or
                not isinstance(record.get('transition_id'), str) or not isinstance(record.get('digest'), str)):
            raise RuntimeRefusal('bad_state')
        allowed = _PREPARE_PHASES if record['action'] == 'prepare' else _SWITCH_PHASES
        if record.get('phase') not in allowed:
            raise RuntimeRefusal('bad_state')
        stored, current = (record['incarnation'], record['epoch']), (body['incarnation'], body['epoch'])
        if stored > current:
            raise RuntimeRefusal('stale')
        if stored < current:
            return None
        if (record['action'] != action or record['transition_id'] != body['transition_id'] or
                record['digest'] != _digest(instruction)):
            raise RuntimeRefusal('conflict')
        return record

    def _release_tunnel(self):
        from jarvis import cluster_tunnel
        if cluster_tunnel.status(self.state)['running']:
            cluster_tunnel.stop(self.state)
            if cluster_tunnel.status(self.state)['running']:
                raise RuntimeRefusal('tunnel_busy')

    def _refuse_older_endpoint(self, body):
        path = self.state/FILE
        if not path.exists():
            return
        prior = verify(_read(path), authority_id=self.participant.client.authority_id,
                       target=self.participant.device_id, pins=self.pins)
        if (prior['incarnation'], prior['epoch']) > (body['incarnation'], body['epoch']):
            raise RuntimeRefusal('stale')
        if (prior['incarnation'], prior['epoch']) == (body['incarnation'], body['epoch']) and prior != body:
            raise RuntimeRefusal('conflict')

    def _require_publication(self, body):
        path = self.state/FILE
        note_path = self.state/'cluster-node.json'
        if not path.exists() or not note_path.exists():
            raise RuntimeRefusal('bad_state')
        saved = verify(_read(path), authority_id=self.participant.client.authority_id,
                       target=self.participant.device_id, pins=self.pins)
        if saved != body or _read(note_path) != _note_for(body):
            raise RuntimeRefusal('conflict' if saved != body else 'bad_state')

    def _require_grant(self, instruction, body):
        path = self.state/GRANT_FILE
        if not path.exists():
            raise RuntimeRefusal('stale')
        record = _read(path)
        if set(record) != {'instruction', 'lease'}:
            raise RuntimeRefusal('bad_state')
        saved = verify(record['instruction'], authority_id=self.participant.client.authority_id,
                       target=self.participant.device_id, pins=self.pins)
        lease = record.get('lease')
        if (saved != body or not isinstance(lease, dict) or
                set(lease) != {'holder', 'lease_id', 'incarnation', 'epoch'} or
                lease.get('holder') != body['main'] or
                any(lease.get(k) != body[k] for k in ('incarnation', 'epoch'))):
            raise RuntimeRefusal('conflict' if saved != body else 'stale')
        snap = self.participant.runtime.lease_snapshot()
        if not snap or any(snap.get(k) != lease[k] for k in ('holder', 'lease_id', 'incarnation', 'epoch')):
            raise RuntimeRefusal('lease_unavailable')

    def _publish_successor(self, instruction, body, lease):
        from jarvis.locking import exclusive_lock
        with exclusive_lock(self.state/'cluster-node.lock'), exclusive_lock(self.state/'cluster-main.lock'):
            _grant(self.participant.client.status(), body)
            record = self._same_phase('prepare', body, instruction)
            if record and record['phase'] in ('released', 'scheduled'):
                # The journal is open, so this phase cannot mean the writer was released.
                raise RuntimeRefusal('bad_state')
            self._refuse_older_endpoint(body)
            self._release_tunnel()
            with exclusive_lock(self.state/'continuity-grant.lock'):
                _save(self.state/GRANT_FILE, {'instruction': instruction, 'lease': lease})
                _save(self.state/FILE, instruction)
                _save(self.state/'cluster-node.json', _note_for(body))
                self._mark('prepare', 'intent', body, instruction)
                # The successor process has to open this same journal. Close only
                # after the grant is durable, and always before the unit starts.
                self.participant.guard.close()
                self._mark('prepare', 'released', body, instruction)

    def _start_successor_unit(self, instruction, body):
        from jarvis import cluster_setup
        report = cluster_setup.role_autostart(self.config_path, self.state, True, restart=True)
        from jarvis.autostart import system
        if system() == 'windows':
            cluster_setup._launch_agent(self.config_path, self.state)
        self._mark('prepare', 'scheduled', body, instruction)
        return {'scheduled': True, 'autostart': report, 'main': body['main']}

    def _retry_released_successor(self, instruction):
        """Unit retry after this process already closed its executor journal.

        Does not reopen the journal, rewrite the grant, or start a unit when
        the saved handover is missing, stale, or still held by a busy tunnel.
        """
        body = verify(instruction, authority_id=self.participant.client.authority_id,
                      target=self.participant.device_id, pins=self.pins)
        if body['main'] != self.participant.device_id:
            raise RuntimeRefusal('not_candidate')
        self._bound()
        fence = self.participant.guard.local_view()
        if (fence.get('quarantined') or fence.get('journal') is not False or
                fence.get('legacy_closed') is not True):
            raise RuntimeRefusal('target_unfenced')
        record = self._same_phase('prepare', body, instruction)
        if record is None or record['phase'] not in ('intent', 'released', 'scheduled'):
            raise RuntimeRefusal('target_unfenced')
        self._require_lease(body)
        from jarvis.locking import exclusive_lock
        with exclusive_lock(self.state/'cluster-node.lock'), exclusive_lock(self.state/'cluster-main.lock'):
            _grant(self.participant.client.status(), body)
            with exclusive_lock(self.state/'continuity-grant.lock'):
                self._require_grant(instruction, body)
                self._require_publication(body)
                if record['phase'] == 'intent':
                    # Close finished and the phase write did not. The writer is already gone.
                    self._mark('prepare', 'released', body, instruction)
                    record = dict(record, phase='released')
            # A scheduled unit is not started again. A tunnel that still holds
            # the hub port is still a refusal, including on that replay.
            self._release_tunnel()
            if record['phase'] == 'scheduled':
                return {'scheduled': True, 'autostart': None, 'main': body['main']}
        return self._start_successor_unit(instruction, body)

    def prepare_successor(self, instruction):
        """Candidate only, after its old node process has stopped cooperatively.

        A grant is kept privately only for this not-ready role handover. This is
        never recovery of a ready lease after a crash or reconstruction from status.
        """
        instruction = copy.deepcopy(instruction)
        self._require_startup()
        try:
            body = self._body(instruction)
        except RuntimeRefusal as err:
            if getattr(err, 'code', None) != 'target_unfenced':
                raise
            return self._retry_released_successor(instruction)
        if body['main'] != self.participant.device_id:
            raise RuntimeRefusal('not_candidate')
        lease = self._require_lease(body)
        self._publish_successor(instruction, body, lease)
        return self._start_successor_unit(instruction, body)

    def _activate_link(self, body):
        from jarvis.linkfinder import activate_tunnel
        activate_tunnel(self.config_path, self.state, body['link'])

    def _start_node_unit(self, instruction, body):
        from jarvis import cluster_setup
        cluster_setup.role_autostart(self.config_path, self.state, True)
        self._mark('switch', 'scheduled', body, instruction)

    def _defer_node_unit(self, instruction, body):
        if not callable(self.after_response):
            raise RuntimeRefusal('response_ack_unavailable')
        self.after_response(lambda: self._start_node_unit(instruction, body))

    def _switch_result(self):
        return {'ok': True, 'authenticated': True, 'key_updated': True, 'endpoint_updated': True}

    def _finish_switch(self, instruction, body, phase):
        """Retry a switch whose tunnel was already handed off.

        Does not stop that tunnel. A down tunnel still refuses before the role
        unit changes. Scheduled means the unit helper already returned.
        """
        _grant(self.participant.client.status(), body)
        if phase == 'scheduled':
            self._require_publication(body)
            return self._switch_result()
        if phase == 'activating':
            self._activate_link(body)
            self._mark('switch', 'tunnel_ready', body, instruction)
            phase = 'tunnel_ready'
        if phase == 'tunnel_ready':
            self.installer.install(instruction)
            self._require_publication(body)
            self._mark('switch', 'published', body, instruction)
            phase = 'published'
        if phase == 'published':
            self._require_publication(body)
            self._defer_node_unit(instruction, body)
        return self._switch_result()

    def switch_node(self, instruction):
        """Publish only after the new endpoint proves its signing key and nonce."""
        instruction = copy.deepcopy(instruction)
        body = self._body(instruction)
        if body['main'] == self.participant.device_id:
            raise RuntimeRefusal('not_candidate')
        record = self._same_phase('switch', body, instruction)
        if record and record['phase'] in ('activating', 'tunnel_ready', 'published', 'scheduled'):
            return self._finish_switch(instruction, body, record['phase'])
        # Prove the endpoint before disturbing the current transport, but defer
        # publishing its credentials until the signed replacement link is live.
        self.installer.verify_endpoint(instruction)
        self._mark('switch', 'intent', body, instruction)
        self._release_tunnel()
        # activating means the old tunnel was released. A later retry must not
        # stop the tunnel this call is about to start.
        self._mark('switch', 'activating', body, instruction)
        self._activate_link(body)
        self._mark('switch', 'tunnel_ready', body, instruction)
        self.installer.install(instruction)
        self._require_publication(body)
        self._mark('switch', 'published', body, instruction)
        self._defer_node_unit(instruction, body)
        return self._switch_result()


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
