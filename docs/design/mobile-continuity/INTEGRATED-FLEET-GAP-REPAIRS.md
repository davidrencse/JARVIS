# Integrated fleet gap repairs

Date: 2026-11-11. Findings 1–5 from `AUDIT-INTEGRATED-REPAIRS.md`. Temp fixtures only. No production config, vault, or live broker. No service restart. No commit. Finding 6 (archive status while a chunk is corrupt) is a different change. Lan Mouse (`jarvis/sharedinput.py`, `jarvis/peripherals.py`, `jarvis/web/sharedinput.js`, `jarvis/web/peripherals.js`) was not modified here.

`FINAL-FLEET-REPAIRS.md` says `settings.write` does not take `fleet-manage.lock` and that putting it on the lock is a follow-up. That sentence is superseded by the Settings section below.

## 1. Settings writes its own keys under the fleet lock

`settings.write` takes `config_mutation` and `patch_config`s only the validated keys onto the file as it is on disk. A `ConfigDrift` between the read and the replace is retried up to three times, then raised. `cluster.*` is written to disk and remembered in `_SAVED_CLUSTER` for the next restart. It is not copied into the running process. `cluster.main_device` is still refused, and that refusal writes nothing. The speaker threshold stays in `voiceprint.json`, under `_WRITE_LOCK`, after the config write, so it does not hold the fleet lock across the invite callback.

The lock is re-entrant on the same thread. An invite already holds it, so a Settings save from `authorize` cannot wait. That save is applied as its own keys. `cluster_setup.invite` reads the file again after `authorize` and again after `enroll(settle_jobs=False)`, before `encode`. If the tunnel is no longer forward, or this node id is not admitted, the attempt restores only the row and key line it still owns. Both restores verified: `InviteIncomplete`, retryable. Either restore unverified: one uncertain receipt, not retryable. Jobs are not marked uncertain on that failure. `settle_rotated` still runs only after a code exists, and a failure there is swallowed.

## 2. One receipt file for every issuance path

The only ledger is `state/cluster-invite-receipts.json`. `uncertain_for`, `assert_node_admitted`, and `note_uncertain_attempt` are the admission API. Console `issue_invite`, `jarvis cluster invite`, and `jarvis cluster enroll` all use it. There is no second file.

The fence is that node id. A different id is still issued. This is not a physical machine and it is not a stop on every invite. `AdmissionBlocked` is mapped to `invite_failed`, `retryable` false, `recover` `revoke`, before the generic `ValueError` wrapper (that wrapper would have marked it retryable).

A successful CLI enroll does not write an issued receipt. The CLI has no `request_id`. Issued-receipt idempotency stays on the console. A second console invite of an enrolled id is `node_enrolled` until revoke. Revoke clears every receipt for that id, including a `cli-*` row.

## 3. `jarvis cluster enroll` rotates only after the pairing file

Order: `assert_node_admitted`, `code_key`, an optional device delta, the bundle without the token, a snapshot of the node row, `Broker.enroll(..., settle_jobs=False)`, `save_json` of the pairing file. If that write fails and the restore verifies, the original error is re-raised, the partial file is removed, and jobs are untouched. If the restore does not verify, one uncertain receipt is recorded and the error says to revoke. `settle_rotated` runs only after the file exists. It updates `state='running'` only. `done`, `failed`, `expired`, and `uncertain` are not put back to `pending`.

`Broker.enroll` still defaults `settle_jobs=True` for other callers. The invite path still passes `settle_jobs=False` and settles after the code.

## 4. Medium — an overdue running attempt is uncertain before the next claim

`running_ambiguous(deadline, now)` is true when `deadline <= now - GRACE` (15 s). `busy_job` still treats the row as busy only while `deadline > now - grace`, so the exact boundary is the same instant. `claim` expires pending rows, then in the same transaction sets this node's running rows at that boundary to `uncertain`, then takes a pending job. It does not update `done`, `failed`, `expired`, or `uncertain`. It does not put the old attempt back to `pending`, so it is not replayed.

`get` uses the same predicate for a running row. A pending row is still late only when `deadline < now`. There is no background sweeper. `running()`, fleet `active_jobs`, and `busy_nodes` do not expire the row. An overdue running row can still be counted until `claim`, `get`, or `recover`.

Inside the grace window, `claim` does not expire the running row and does not refuse a second claim. `Hub.dispatch` refuses while `busy_job` is set. A direct second claim while both deadlines are still inside the window can still leave two running rows. That was already true, and it is not the filed hole. The filed hole was the boundary, where `busy_job` was already empty and `claim` still left the first row `running` and took another. `connections.presence` still says a node runs one job at a time. That sentence is true only while the running attempt is inside the window.

## 5. Medium — busy is not labeled offline

`settings.machines` already returned `online` false, `presence` `busy`, `paired` true. The renderers were dropping `presence`.

- Settings `machineStatus`: tone `warn`, "Busy, awaiting contact". Offline stays "Offline" and tone `down`.
- Screen `screenHold`: `{enabled: false, text: "Busy, awaiting contact"}`. The Screen button uses `hold.enabled`, so it stays disabled. `snapScreen` returns without a capture when presence is busy. Live and Snapshot stay disabled. The header still counts `n.online` only. `reachable()` stays online-only, so the first automatic snapshot stays off for a busy node. Make-main stays the unavailable toast.
- Phone `askTargetChoice`: the option stays disabled, labeled " (busy, awaiting contact)". Offline stays " (offline)" and stays disabled. An online option stays enabled. The device dot is still the online bit. The detail line already said busy.

Phone fleet `status` stays `link_state` on purpose. `presence` carries busy on both audiences. Owner fleet `status` uses the presence word when the link is online or offline. `link_state` and `Hub.alive` stay different questions. `Hub.alive` still treats `busy_job` as ready so dispatch does not fall back to SSH.

## Tests

`tests/cluster/test_integrated_fleet_gaps.py` (temp dirs, patched signing and sshd):

| Test | What it pins |
| --- | --- |
| `SettingsWriteTests.test_load_hook_set_main_is_not_written_back` | Hook `config_lock.json.loads` once, `set_main` to dell inside that read, then save `voice.kokoro_speed` 1.1. Disk main stays dell, voice is 1.1, `keep.me` stays. Memory voice updates. Memory main stays the process copy. |
| `SettingsWriteTests.test_stale_memory_does_not_restore_main` | `set_main` finished first. A later voice save from memory that still says zenbook does not write zenbook back. |
| `SettingsWriteTests.test_write_is_busy_while_the_fleet_lock_is_held` | Another thread holds the lock, timeout 0.2 s. Tunnel save raises `ConfigBusy`. Disk and memory stay `forward`. |
| `SettingsWriteTests.test_tunnel_is_saved_for_restart_and_main_is_refused` | Tunnel `reverse` is on disk and in `_SAVED_CLUSTER`. The running config stays `forward`. `cluster.main_device` still raises and does not change the file. |
| `InviteAdmissionTests.test_authorize_tunnel_save_does_not_return_a_code` | `authorize` saves tunnel `reverse`. No code, enroll not called, no node row, prior SSH line kept, disk tunnel is `reverse`, `keep.me` kept. Retryable, because the cleanup verified. |
| `InviteAdmissionTests.test_uncertain_receipt_blocks_only_that_node_id` | Planted uncertain receipt for `oracle1`. Console replace, `cluster_setup.invite`, and `jarvis cluster enroll` raise before `code_key`. `pixel9` still receives a code. The only receipt file is `cluster-invite-receipts.json`. |
| `EnrollOrderTests.test_code_key_failure_does_not_rotate` | `code_key` raises. Job stays `running`, old token authenticates, pairing file absent, enroll not called. |
| `EnrollOrderTests.test_pairing_write_failure_restores_the_token_and_leaves_jobs` | `save_json` raises after enroll. Original `OSError` comes back. Old token works. Running stays running. Done stays done. `settle_rotated` not called. No uncertain receipt. |
| `EnrollOrderTests.test_running_jobs_settle_only_after_the_pairing_file` | `code_key`, then enroll with `settle_jobs` false while the file is absent and the job is still running, then `settle_rotated` only once the file exists. Running becomes `uncertain` only then. Done stays done. New token works. Old token does not. |
| `EnrollOrderTests.test_default_enroll_still_settles_running_jobs` | `Broker.enroll` without the flag still marks a running job uncertain and leaves a done job done. |
| `DeadlineTests.test_exact_grace_boundary_matches_busy_and_is_not_replayed` | Clock frozen at `T`. `deadline == T - GRACE`: `busy_job` is empty, `running()` still counts the row, `claim` marks it uncertain and takes the pending job, a later claim does not resurrect it, a done job with deadline 0 stays done. `get` at that instant returns uncertain. `running_ambiguous` is true at `T - GRACE` and at `T - GRACE - 0.001`, false at `T - GRACE + 0.001`. |
| `DeadlineTests.test_inside_the_window_stays_running_and_a_second_claim_still_works` | `deadline == T - GRACE + 0.001`: `busy_job` is set, `get` stays running, `claim` with nothing pending returns none and leaves it running. Two claims while both deadlines are `T + 60` both return a job and both stay running. |
| `SurfaceWordingTests.test_settings_screen_and_phone_picker_say_busy_and_stay_disabled` | `settings.machines` on a 100 s-old check-in with a live running job: online false, presence busy, paired true. Node runs the three helpers. Busy settings text is not "Offline". `screenHold` busy is disabled. Offline screen stays disabled. Online screen stays enabled. The ask option is disabled with " (busy, awaiting contact)", not " (offline)". Offline ask stays disabled. The Screen button template still disables on `hold.enabled`. `snapScreen` still returns on busy. `node --check` on the three JS files. |

`tests/cluster/test_cluster_setup.py` `InviteRollbackTests.test_enroll_failure_removes_only_the_new_key_line` accepts `settle_jobs` and requires the message "db down". The old signature TypeError'd once invite passed `settle_jobs=False`, and the generic handler still looked retryable.

This agent runs those modules plus `tests.cluster.test_config_writers`, `tests.cluster.test_cluster_setup`, `tests.cluster.test_fleet_management`, `tests.runtime.test_settings`, `tests.cluster.test_surface_liveness`, `tests.security.test_permissions`, `tests.cluster.test_audit_cluster.GraceTests`, and `tests.cluster.test_cluster`. The parent runs the full suite after every agent finishes. `JARVIS_NO_MODELS=1`, `.venv/bin/python`, no browser pass.

## Remaining limits

- No MAIN lease, epoch, or cross-machine fence. The lock is per state folder on this machine. Two config copies can still both read as main.
- The uncertain fence is the configured node id, not a physical computer. Issuing a different id is allowed.
- A device label written before a later invite step fails can remain. The credential and the new key line do not, when the restore verifies.
- `settle_rotated` after a code or a pairing file exists is best effort. If it fails, the old token is already dead and the job stays `running` until `get` or `recover`.
- An ambiguous running row stays `running` on read paths until `claim`, `get`, or `recover`.
- Inside the grace window, a second direct `claim` is still allowed. Dispatch will not do that while `busy_job` is set.
- Same-thread Settings save during an invite is applied. The invite does not return a code when the re-read fails. The tunnel change itself is kept.
- `jarvis/web/fleet.js` checkbox "I checked. Make a new request for it." still sends a new `request_id` and `replace_pending`. It does not call revoke. The server refuses with `recover=revoke`.
- `jarvis/web/remote.js` still tells the operator to pair with `jarvis cluster invite`. That path now consults the receipt file. The string was not edited here.
- Phone fleet `status` stays `link_state` (`offline` while presence is `busy`). Phone `can_write` stays false.
- Hand edits and any writer that does not take the lock are still outside it. The compare-and-swap only narrows that window.
- Finding 6 is not this change. Lan Mouse files are not this change.

## Safe activation

Restart the console and the hub on the main so they load this Python. Nodes need the usual code update for anything they run locally. These JS files load with the console page. No config, database, or receipt migration. An existing `cluster-invite-receipts.json` is read as it is.
