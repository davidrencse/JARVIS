# Fleet API

*Owner-console and phone contract for node invite, revoke, capabilities, and the read-only fleet picture. Code: `jarvis/fleet_management.py`, routes in `jarvis/ui.py` and `jarvis/phone.py`.*

Screen copy and layout live in [docs/handbook/fleet-ui.md](../handbook/fleet-ui.md). Continuity handoff is [main-handoff.md](main-handoff.md). This API wraps `cluster_setup.invite` / `revoke` and `Broker.set_capabilities`. It builds no remote script and adds no new power.

## Callers

| Caller | Reads | Writes | Main transfer |
| --- | --- | --- | --- |
| Owner console: loopback `Host` allow-list, `Sec-Fetch-Site` `same-origin` or `none` (or absent), POST also `X-JARVIS: 1` | yes | `invite`, `revoke`, `capabilities` only when this PC is the enabled main | Preview is read-only. POST runs `set_main` only under the conditions below |
| Node token, any capabilities | 403 on every spelling | 403 | 403 |
| Paired phone `GET /phone/api/fleet` | safe projection | no route (404) | none |

A paired phone is not fleet admin. Phone fleet writes are not built. All `/api/fleet*` replies send `Cache-Control: no-store`. Fleet errors are `{error, code, retryable, step?, …}`. Switch on `code`.

`actions[].support`: `supported` (button), `guide` (text only), `blocked` (disabled, show `why`), `unsupported` (`cloud_provisioning` only: JARVIS does not create cloud servers or install an OS). On a node console, or with the cluster off, owner writes are `blocked` in the payload and 409 `not_main` or `cluster_off` if called anyway.

## Lock, receipts, cloud

Every managed config write goes through `config_lock.config_mutation` on `<state>/fleet-manage.lock`: console fleet, settings, and `jarvis cluster configure`, `main`, `init`, `invite`, `enroll`, `join`, and `leave`. The latest file is re-read under the lock. Only the caller's keys are applied. A hand edit does not take the lock. The compare before the rename narrows that window. It does not close it. The lock is one machine. It is not a lease and it fences no other PC. Same-thread re-entry is deliberate so console invite can call `cluster_setup.invite`.

The only invite ledger is `<state>/cluster-invite-receipts.json`: request id, node, state `pending|issued|uncertain`, and a digest. No code, no token, no private key. There is no second file and no in-memory ledger. Receipt read-modify-write uses the same lock and merges that caller's delta, so a stale snapshot cannot drop another writer's fence.

An uncertain receipt blocks every new invite or enroll for that configured node id until `jarvis cluster revoke` or the console revoke. Revoke clears that node's receipts only after `Broker.revoke` returns. If revoke raises, the fence stays. A different node id is not blocked. The fence is the configured id, not a physical machine.

Cloud profile is inventory only, `main_eligible` false, `main_candidate` `never`. Do not call a cloud node a gateway. MAIN is never an enroll, revoke, or capability target.

Profile defaults: `pc` → desktop, inventory, grokbot, files. `headless` and `cloud` → inventory only. Anything beyond the profile default needs the same ids in `acknowledge`, or 400 `capability_ack_required` with `needs_ack`. The UI shows capability titles. The id stays in the payload. Gating is unchanged. `camera` also needs `jarvis camera allow on` on the node.

`cluster_store.ONLINE` is 75 seconds. `GRACE` is 15 seconds. A running job counts as busy only while `deadline > now - 15`. At `deadline <= now - 15`, `running_ambiguous` is true: callers mark the attempt uncertain and never put it back to pending. Presence vocabulary for the cards is in [parity-console-phone.md](parity-console-phone.md). `link_state()` stays the invite and enrolled guard and is a different function.

## Reads

`GET /api/fleet` returns `{audience:"owner", can_write, cluster:{enabled, local, main, local_is_main}, profiles, capabilities, devices, actions, guide, checked_at}`.

Device fields: `id`, `label`, `role` `main|node`, `profile`, `profile_label`, `platform`, `status` (`main`, `not_enrolled`, `invite_pending`, `invite_expired`, `waiting_for_first_contact`, `online`, `offline`, `unconfigured`), `online`, `seen`, `main_eligible` (`false` or `"blocked"`), `main_eligible_why`, `capabilities` (`id`, `title`, `risk` `low|medium|high`, `explain`, `default`), `step`. Owner-only: `invite_expires`, `hostname`, `address`, `active_jobs`, `code_ok`.

`unconfigured` means enrolled on the hub and missing from the config. Offer revoke. The read uses the broker sqlite read-only. It never creates the file and never returns token hashes or codes.

`GET /api/fleet/vault` is numeric and body-free. Local `git` only, fixed arguments, timeout 5–8 seconds, cached 15 seconds, no fetch, no network, configured vault path only.

`{checked_at, network:false, archive, vault:{configured, exists, write_enabled, is_git}, git|null, visibility:{value:"unknown", note}, problems}`. Archive fields are the allow-list from second-brain status (`enabled`, `stage`, `state`, `gate`, pending, held, errors, `capture_complete`, `capture_enabled`, unreconciled, source-gone, oldest pending age, last success, queue, `has_error`). Git may include `branch`, `detached`, `remote:{name:"origin", provider:"github|other", host, repo, credentials_embedded}`, `upstream_set`, `ahead`, `behind`, `ahead_behind_as_of:"last fetch"`, `last_fetch_at`, `dirty`, `clean`, `connected`. Never returned: remote URL, filenames, note content, the vault path, `last_error` text. `connected` means a local `origin` on github.com exists. It is not proof the credentials work. `visibility` is always unknown here. Private/public and fresh ahead/behind need a network fetch, which this route does not do. UI words for archive states are in [parity-console-phone.md](parity-console-phone.md). `capture_complete` must not be rendered as "capture complete".

## Preview

`POST /api/fleet/preview` body `{node, profile?="pc", os?, addresses?, capabilities?, label?}`.

Returns `{node, label, profile, os, ready, problems:[{code, why}], checks:[{id, state:"ok|problem|unknown", why}], steps:[{id, title, kind:"auto|action|guide", state:"todo|pending|waiting|blocked|done", action?}], link, capabilities, needs_acknowledge, would_change:{config, authorized_keys, credential, network}, addresses, node_commands, limits, mutated:false}`.

`node_commands` are fixed strings per OS (`linux`, `macos`, `windows`) with a literal `<INVITE CODE>` placeholder. They are never executed. The preview probes loopback sshd. It never calls `invite()`.

## Invite

`POST /api/fleet/invite` body `{request_id, node, profile?, os?, label?, addresses?[<=4], capabilities?, acknowledge?, replace_pending?}`. `request_id` is required, 8–64 characters `[A-Za-z0-9_-]`. Unknown fields are 400.

Success 200: `{ok, node, profile, label, capabilities, addresses, expires_at, expires, code, secret:true, node_commands, config_changed, device, next, step:"join"}`.

`code` (`jarvis-invite-1:…`) is a password. It is shown once, in this reply only. Never log it, store it, put it in a URL, or persist it in the browser. Inventory, preview, and the phone never return it.

- The same `request_id` never issues a second code, including after a restart: 409 `already_issued`. The code cannot be recovered. Replacing an invite needs `replace_pending:true` and a new `request_id`. `replace_pending` on an id that already issued, or that is `pending` while the credential row still exists, does not rotate anything.
- `uncertain` is 409 `invite_failed`, `retryable:false`. A `pending` receipt with no credential row may be retried with the same id. A clean failure before a credential exists deletes that pending receipt.
- Already joined (`online`, `offline`, `waiting_for_first_contact`): 409 `node_enrolled`. Revoke first. An unused unexpired invite is 409 `invite_pending` unless `replace_pending:true`. An expired unused invite may be re-issued.
- Order: validate → require main → lock → receipt check → conflicts → readiness (forward tunnel, sshd) → write `pending` → config device entry → re-read the config, require MAIN and a forward tunnel again → `cluster_setup.invite`. The second read is a config check, not a lease. A role change after it is still unseen.
- Under the lock only `devices.<node>` changes. Existing keys are kept. `profile` is set. Backup `<config>.pre-fleet-onboarding-<stamp>` is written first, only when the file changes, nanosecond stamp so two backups in the same second do not collide.
- Signing key, host key, and the node key pair are made before the lock. `invite` never deletes a credential that was already there. When rollback of this attempt's credential and key line is verified, 409 `invite_failed` is `retryable:true` and the same `request_id` may be retried. When it cannot be verified, the receipt is `uncertain`, `retryable` is false, and the error does not claim the invite was atomic. The device entry stays.
- The bus gets `fleet.changed {action, node}` only. `config_changed` is retryable.

400: `bad_body`, `bad_node`, `bad_request_id`, `bad_profile`, `bad_os`, `bad_address`, `bad_label`, `bad_capabilities`, `bad_acknowledge`, `bad_replace`, `unknown_fields`.

409: `main_not_allowed`, `cluster_off`, `not_main`, `node_enrolled`, `invite_pending`, `already_issued`, `tunnel_forward`, `sshd` (retryable), `profile_mismatch`, `invite_failed` (retryable when rollback was proved), `busy` (retryable).

500: `config_unreadable`, `state_unwritable`.

`jarvis cluster invite` and `jarvis cluster enroll` use the same receipt file. Enroll checks again under the lock after the signing key and any device delta, and before it rotates a credential. A role flip during signing-key generation is seen on that second check and does not leave a new credential.

## Revoke and capabilities

`POST /api/fleet/revoke` body `{node, confirm}` with `confirm` equal to `node`. Returns `{ok, node, was_enrolled, ssh_key_removed, active_jobs_cancelled, warning?, step:"invite"}`. Cuts the credential and the SSH key. Main is `main_not_allowed`. Idempotent for an unenrolled node. Frees the id for a fresh invite.

`POST /api/fleet/capabilities` body `{node, capabilities, acknowledge?}`. Replaces the set through `Broker.set_capabilities`. The credential is not rotated (`credential_rotated:false`). Returns `{ok, node, changed, added, removed, capabilities, note?}`. `unknown_node` and `not_enrolled` are 404. `main_not_allowed` refuses the main. Revoke and capability writes re-read the config under the lock, the same way invite does. A stale MAIN role or a changed profile cannot silently authorize the write. That is still config-level protection, not a lease.

## Main transfer

`POST /api/fleet/main-transfer/preview` body `{target}` is read-only. It never changes `cluster.main_device` and it never calls the handoff.

Readiness checks: `known_device`, `not_current_main`, `not_cloud`, `online`. Without an explicit config file and a bound handoff, `status` and `support` stay `blocked`, and `missing` is the unfinished-handoff list (`authority_store`, `authority_lease`, `fencing`, `target_ack`, `executor_guards`, `fresh_owner_proof`, `checkpoint_barrier`). With both, and a known non-cloud target that is not already main, on the enabled local main, `support` is `supported`. `status` is `ready` when that target is also online, and `blocked` when it is offline. Offline copy says a missed check-in is not readiness. Ready copy says the preview changed nothing and that POST runs the coordinator, which still checks candidate readiness, lease expiry, and the safety barrier. `mutated` is always false. Risks stay: two machines acting as main could both write memory and run jobs; editing `cluster.main_device` by hand moves no data and fences nothing.

`POST /api/fleet/main-transfer` calls `main_transfer_refuse`.

- A missing or non-absolute `config_path`, or a path that is not an existing file, is 409 `blocked`. It does not look for a checkout config.
- Otherwise, on the enabled local main, it calls `set_main`. Pending or failed does not change the role: 409 `pending` or `blocked`, `mutated:false`, with the handoff status attached.
- Success returns only when `ok` is true, `mutated` is true, and `main` equals the target: `{ok:true, status, mutated:true, target, main, main_device, transition_id, epoch, publication, authority}`. It does not suggest a restart.
- `publication_conflict` and `publication_failed` are 409. `publication_failed` is retryable.

`set_main` itself takes `config_mutation`, re-reads the file, and writes `cluster.main_device` only when the bound handoff says the old lease is gone and the safety barrier is clear. A callback that says ok is not enough. A later retry that already matches the stored receipt finishes the config write and does not call the handoff again. With no bound handoff, `set_main` refuses and does not rewrite the role. Settings uses that same function when `cluster.main_device` is the only key in the save.

Older sentences that say this POST is always 409, or that CLI invite is unlocked, do not match the current code.

## Phone projection

`GET /phone/api/fleet` uses the same inventory with `audience:"phone"`, `can_write:false`, `cluster.local:null`. No hostname, address, invite expiry, `code_ok`, or `locate`. `status` does include `invite_pending` and the other link states, and `step` is present. Write actions are `blocked` with no `path`. There is no preview action. No subprocess and no probe. Any `POST /phone/api/fleet…` or phone path under `/api/fleet/` is 404 (405 when the method is not POST-shaped).

## Say this in the UI

- Safe handoff is configured only when preview says `support:"supported"`. That is not proof the target can take MAIN.
- The phone cannot invite, revoke, or move MAIN.
- Cloud provisioning is unsupported.
- The node side of onboarding is a command the user runs. The preview shows it. JARVIS does not run it.
- GitHub visibility and fresh ahead/behind are not known from this API.
- Nothing here commits or pushes the vault.
