# Final fleet repairs: managed config lock, invite ordering, receipt ledger

Date: 2026-11-11. Scope: correctness repairs for the findings in `AUDIT-CONSOLIDATION-BACKEND.md`
(stale `_write_device` clobber, role flip during `code_key`, unlocked CLI writers, job state on a
failed re-invite) and the duplicate in-memory/JSON invite ledger. Not a rewrite.

Not implemented here, and not claimed: lease fencing, a durable main arbiter / Oracle handoff, or a
C3 canonical read. Two machines with diverging `jarvis.config.json` copies can still both read as main
(`tests/runtime/test_harness_baseline.py` still documents that).

## What changed

### `jarvis/config_lock.py` (new, registered in `sysview.py`, station "log")
One lock and one write path for every *managed* writer of `jarvis.config.json`.

- `config_mutation(state, timeout=None)`: cross-process `flock`/`msvcrt` lock on
  `<state>/fleet-manage.lock` (the same file the fleet lock already used, so old and new code agree).
  Re-entrant per thread, so the console invite can hold it and call `cluster_setup.invite`, which
  takes it again, with no self-deadlock. Other threads and processes are serialised. Busy after
  `DEFAULT_TIMEOUT` (5 s) → `ConfigBusy` (`code = "busy"`).
- `patch_config(state, path, change, *, guard, backup, create)`: under the lock, re-reads the file,
  runs `guard(latest)` (a role/tunnel check), applies only the caller's change to the latest
  document, writes a backup that can't collide (`<name>.pre-<tag>-<time>-<ns>-<rand>`, `O_EXCL`), then
  compares the bytes on disk again just before the atomic replace. If the bytes changed, it raises
  `ConfigDrift` (`code = "config_changed"`) and writes nothing.
- `save_delta(state, path, before, after, ...)` / `merge_changes`: for callers that hold a whole
  proposed config (configure, init, join, enroll). Only the keys that differ between `before` and
  `after` are applied to the latest file. If the disk has a different value for one of those keys, it
  raises `ConfigDrift`.

### Writers now on that path
| Writer | File | Backup tag |
|---|---|---|
| Console invite device entry (`_write_device`) | `fleet_management.py` | `fleet-onboarding` |
| `jarvis cluster invite` (new device entry) | `cluster_setup.invite` | `fleet-onboarding` |
| `jarvis cluster main` (`set_main`, with or without `state`) | `cluster_cli.py` | `cluster-main` |
| `jarvis cluster configure` (guard: identity + endpoint) | `cluster_cli.py` | `cluster-configure` |
| `jarvis cluster enroll` (new device) / bundle `join` | `cluster_cli.py` | `cluster-enroll` / `cluster-join` |
| `cluster init` / `join` / `leave` | `cluster_setup.py` | `cluster-init` / `cluster-join` / `cluster-leave` |

### Invite ordering (`cluster_setup.invite`)
1. The expensive and failure-prone material is made first, **outside** the lock: `code_key`, `host_key`,
   `keypair`, and the authorized_keys path. A failure there changes nothing and can be retried.
2. Then, under `config_mutation`, `_invite_locked` re-reads the config on disk and runs `_role_guard`:
   this PC must still be the main, the node must not be the main, and the tunnel must be forward. A
   `cluster main dell` that landed while the key was being made is refused here: "This PC is no
   longer the main; nothing was changed."
3. The device entry is added with `patch_config`, which writes only `devices.<node>` and so can't
   clobber anything else. Then authorize, `Broker.enroll(..., settle_jobs=False)`, encode, and the
   optional output file.
4. Jobs are marked `uncertain` **only after the commit**, by `Broker.settle_rotated(node)`
   (running → uncertain, nothing else). A failed or rolled-back invite never touched job state, so
   nothing has to be "restored". A job that finished during the attempt stays `done`, so a finished
   job is never resurrected. Rollback still restores only the row and key line this attempt owns, and
   keeps the previous SSH line and token.

`Broker.enroll` keeps `settle_jobs=True` by default, so other callers (`cluster enroll`) behave as before.

### One ledger authority (`fleet_management.py`)
- The in-memory `_LEDGER` is gone. `state/cluster-invite-receipts.json` is the only ledger: request
  id, node, state, digest, and never a code or key. The rules live in one place, `issue_invite`.
- Same request id after a lost reply or a restart → `already_issued`.
- An `uncertain` receipt for a node blocks **every** new request for that node, including a new
  request id or `replace_pending`. The reply is `invite_failed`, `retryable: false`, `recover: "revoke"`.
  The explicit reconciliation is revoking the node, which clears its receipts. Then a fresh invite works.
- A failed `issued` receipt write → `retryable: false`, and the pending receipt stays, so it is never
  issued twice.
- If the console invite fails before any credential exists (busy, drift, role refusal), its pending
  receipt is dropped and the same request id can retry.

## Error codes for the Console UI (no JS changed here)
| code | status | retryable | meaning |
|---|---|---|---|
| `busy` | 409 | yes | another managed config/fleet change holds the lock |
| `config_changed` | 409 | yes | the file changed under us (editor/unmanaged writer); nothing written |
| `config_unreadable` | 500 | no | config missing/not a JSON object |
| `not_main` / `tunnel_forward` | 409 | no | role or tunnel on disk no longer allows inviting |
| `already_issued` | 409 | no | this request id already issued a code |
| `invite_failed` + `recover: "revoke"` | 409 | no | an earlier attempt for this node is uncertain |
| `invite_failed` | 409 | yes | nothing was changed (e.g. signing key failed); retry is safe |

CLI callers see the same failures as `ValueError` messages (`ConfigBusy`, `ConfigDrift`,
`InviteIncomplete`).

## Tests (permanent repros)
- `tests/cluster/test_config_writers.py` (new, 25 tests):
  - delta merge keeps unrelated edits; a conflicting key is refused;
  - an editor writing between read and replace → drift, its edit kept;
  - unique backups; a no-op writes nothing;
  - re-entrancy and thread exclusivity; another **process** holding the lock → busy for
    `config_mutation` and `set_main`;
  - `configure` waits on a held lock and keeps the holder's new device; `set_main` keeps newer
    devices and checks the device on disk; `leave` only flips `enabled`;
  - role flip and tunnel flip during `code_key` → no row, no key line, no device entry;
  - a stale caller config doesn't clobber; the CLI invite is busy while the lock is held and works after;
  - jobs become uncertain only after a successful rotation; a failed re-invite leaves a running job
    running with the old token valid; a job finished during cleanup stays `done`;
  - console invite end to end with the real `cluster_setup.invite` (keys patched);
  - an uncertain attempt blocks the same node across new ids until revoke;
  - an issued-receipt write failure is never reissued; repeat after a "restart" is refused from the
    file alone; busy → 409 retryable;
  - audit `clobber_between_read_and_write` as a fleet test (role flip between read and replace →
    `config_changed`, nothing written, then `not_main`).
- `tests/cluster/test_fleet_management.py`: `_LEDGER` references removed; the flip helpers accept the
  new keywords; the set_main busy test expects `ConfigBusy`.
- Focused run: config_writers, fleet_management, cluster_setup, cluster_roles, harness_baseline,
  sysview and cluster store tests: **185 OK**. The full suite was not completed in this session; the
  parent runs it.
- The audit script `/tmp/jarvis-consolidation-backend-audit.py` calls the removed `fm._LEDGER.clear()`
  and patches `shutil.copy2`, so it no longer runs unmodified. A copy with that call removed reports:
  role_flip_during_code_key refused (main dell, 0 rows); code_key_before_enroll 0 rows and keys
  unchanged; receipt_survives_ledger_clear `already_issued`; failed_reinvite job `running` and token
  restored. Its `flip_before_write_read` mock lacks the new `state=` keyword (repro-harness issue, now
  covered by `test_main_flipped_during_device_write_does_not_invite`), and its clobber hook no longer
  fires (covered by the new fleet test above).

## Remaining limits (honest)
- **Hand edits and unmanaged writers are not fenced.** An editor, or `settings.write` (Settings →
  cluster.tunnel etc., which serialises on its own thread lock and never takes this lock), can still
  write at any time. The compare before the replace narrows that window to the instant between the
  compare and the rename; it does not close it. `settings.write` already refuses `cluster.main_device`.
  Putting `settings.write` on `config_lock` is a one-line follow-up for its owner.
- No lease, epoch or cross-machine fencing: the lock is per machine (per state folder).
- A device entry `{"label": node}` that the CLI invite added stays in the config if a later step fails
  (it was like that before too; the console path writes its richer entry first by design).
- `settle_rotated` is best effort after the commit. If it fails, a job that was running under the dead
  credential stays `running` until the normal job expiry; it can't report back with the old token.
- An `uncertain` node needs a revoke to recover; there is no automatic reconciliation.

## Safe activation
1. Python changes: restart the JARVIS console/hub process on the main (David does this). Nodes need
   `jarvis cluster update <node>` for the code fingerprint. Offline nodes: leave them.
2. No config, DB or state migration. Existing `cluster-invite-receipts.json` files are read as they
   are. An existing `fleet-manage.lock` is reused.
3. After the restart, check with `jarvis cluster roles` (read only). To exercise the lock without
   inviting anyone, `jarvis cluster main <current-main>` writes nothing when nothing changes.
