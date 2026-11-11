# Fleet management API (backend contract)

Code: `jarvis/fleet_management.py`. Routes: `jarvis/ui.py` (owner Console) and
`jarvis/phone.py` (paired phone, read-only). Tests: `tests/cluster/test_fleet_management.py`
(57), `tests/mobile/test_phone_fleet.py` (7).

Purpose: add a PC, headless computer or cloud server (Oracle etc.) as a **node** with the
existing invite/join machinery, see links and capabilities, and see the Obsidian archive and
GitHub link. It adds **no new power**: it wraps `cluster_setup.invite/revoke` and
`Broker.set_capabilities`. No remote script is ever built or run.

## Permission model

| Caller | Reads | Writes (`invite`, `revoke`, `capabilities`) | Main transfer |
|---|---|---|---|
| Owner Console (`Host` allow-listed, `Sec-Fetch-Site` same-origin/none, `X-JARVIS: 1` on POST) | yes | yes, **only when this PC is the main and `cluster.enabled`** | preview only; execute = 409 `blocked` |
| Node token, any capabilities | **403** (hub `node_api` default-deny, every spelling) | 403 | 403 |
| Paired phone (`/phone/api/fleet`) | safe projection | none: no route (404) | none |

A paired phone is not fleet admin. Writes from a phone need the future fresh-owner-proof
(`manage_main` / passkey); not built.

All `/api/fleet*` replies carry `Cache-Control: no-store`. Error replies from fleet routes are
`{"error": str, "code": str, "retryable": bool, "step"?: str, ...}`; **switch on `code`**, show
`error`.

## Support levels (frontend must distinguish)

`actions[].support` in the inventory:

- `supported`: button works (owner Console on the main).
- `guide`: read-only instructions, no button that changes anything.
- `blocked`: not available now; `why` says what is missing. Render disabled with the reason.
- `unsupported`: JARVIS will never do it (`cloud_provisioning`: it does not create cloud servers
  or install an OS; the user creates the server, then adds it as a node).

## Reads

### `GET /api/fleet`
```
{audience:"owner", can_write:bool,
 cluster:{enabled, local, main, local_is_main},
 profiles:[{id:"pc|headless|cloud", label, about, default_capabilities:[id], main_candidate:"later|never"}],
 capabilities:[{id, title, risk:"low|medium|high", explain}],
 devices:[{id,label,role:"main|node",profile,profile_label,platform,
           status:"main|not_enrolled|invite_pending|invite_expired|waiting_for_first_contact|online|offline|unconfigured",
           online,seen(epoch|null),
           main_eligible:false|"blocked", main_eligible_why,
           capabilities:[{id,title,risk,explain,default}],
           step,                       // plain "what to do next"
           invite_expires?, hostname?, address?, active_jobs?, code_ok?}],   // owner only
 actions:[{id,support,why,method?,path?,owner_only?,secret_in_reply?,needs_confirm?}],
 guide:[{id,title,who,body}],         // 5 onboarding steps
 checked_at}
```
Reads the broker's sqlite read-only: never creates it, never returns token hashes or codes.
`unconfigured` = enrolled on the hub but missing from the config (offer revoke).

### `GET /api/fleet/vault`
Numeric, body-free. Local `git` only (fixed args, 5-8 s timeout, cached 15 s, no fetch, no
network), on the **configured vault path only**.
```
{checked_at, network:false,
 archive:{enabled,stage,state,gate,archive_pending,archive_held,archive_errors,capture_complete,
          capture_enabled,archive_unreconciled,archive_source_gone,oldest_pending_age_s,last_success,
          queue,has_error ...},                       // allow-listed from second_brain_runtime.status
 vault:{configured,exists,write_enabled,is_git},
 git:{available,branch,detached,
      remote:{name:"origin",provider:"github|other",host,repo:"owner/name"|null,credentials_embedded},
      upstream_set,ahead,behind,ahead_behind_as_of:"last fetch",last_fetch_at,
      dirty:{staged,modified,untracked,conflicted},clean,connected}|null,
 visibility:{value:"unknown",note},                  // private/public needs the network: never claimed here
 problems:[str]}
```
Never returned: remote URL, filenames, note content, the vault path, `last_error` text.
`connected` means an `origin` on github.com exists locally; it is not proof the credentials work.

## Preview (no mutation)

### `POST /api/fleet/preview`  body `{node, profile?="pc", os?, addresses?, capabilities?, label?}`
Returns `{node,label,profile,os,ready,problems:[{code,why}],checks:[{id,state:"ok|problem|unknown",why}],
steps:[{id,title,kind:"auto|action|guide",state:"todo|pending|waiting|blocked|done",action?}],link,
capabilities,needs_acknowledge:[cap],would_change:{config,authorized_keys,credential,network},
addresses,node_commands:[{label,command|null}],limits:[str],mutated:false}`.
`node_commands` are fixed strings per OS (`linux|macos|windows`) with a literal `<INVITE CODE>`
placeholder, never executed. Does a loopback sshd probe; never calls `invite()`.

## Owner writes (Console, main only)

### `POST /api/fleet/invite`
Body: `{request_id (required, 8-64 [A-Za-z0-9_-]), node, profile?, os?, label?, addresses?[<=4],
capabilities?, acknowledge?[cap], replace_pending?:bool}`. Unknown fields → 400.

Success `200`:
```
{ok, node, profile, label, capabilities:[...], addresses, expires_at(epoch), expires,
 code:"jarvis-invite-1:...",   // SECRET: show once, never log/store/put in URL; no browser persistence
 secret:true, node_commands:[...], config_changed, device, next, step:"join"}
```
Rules:
- Capabilities beyond the profile default (pc: desktop/inventory/grokbot/files; headless and cloud:
  inventory only) need the same ids in `acknowledge`, else 400 `capability_ack_required`
  (`needs_ack:[...]`). Cloud/headless never get desktop/camera/locate/terminal by default.
- **Idempotency / lost reply.** The same `request_id` never issues a second code: 409
  `already_issued`. The code can't be recovered; the UI must tell the user to create a new
  invite with `replace_pending:true` and a **new** `request_id`. `replace_pending` on the id that
  already issued, or that is `pending` while the node's credential row still exists, does not
  rotate anything. A receipt of that fact is `state/cluster-invite-receipts.json` (request id,
  node, state `pending|issued|uncertain`, and a digest; no code, no token, no private key). It
  is written with a lock, an atomic replace and fsync, and it survives a process restart.
  `uncertain` (cleanup could not be proved) is 409 `invite_failed` with `retryable:false`.
  A `pending` receipt with no credential row may be retried with the same id. A clean failure
  before a credential exists deletes that pending receipt. An in-memory ledger is only a cache.
- Node already joined (`online|offline|waiting_for_first_contact`): 409 `node_enrolled`. Never
  rotates a live credential; revoke first. An unused unexpired invite: 409 `invite_pending`
  unless `replace_pending:true`. An expired unused invite may be re-issued directly.
- Order: validate → require main → lock → receipt check → conflicts → readiness (tunnel forward,
  sshd) → write `pending` receipt → config device entry → **re-read the config file, require MAIN
  and a forward tunnel again** → `cluster_setup.invite`. That second read is a config check, not
  a lease or a fence: a role change after it is still unseen. `jarvis cluster main` takes the
  same lock when it is given a state folder and re-reads the file before writing `main_device`.
  `jarvis cluster configure` and a hand-edit do not. The CLI `jarvis cluster invite` does not
  take this lock. Config: re-read under the lock, only `devices.<node>` changed (existing keys
  kept; `profile` set), backup `<config>.pre-fleet-onboarding-<stamp>` made first only when the
  file changes, same format and mode as `save_json`.
  `invite` prepares the code-signing key and the SSH host key before it authorizes, enrolls or
  writes the code. If a later step fails, it deletes or restores only the credential and key
  line this attempt still owns. It never deletes a credential that was already there. When that
  rollback is verified, 409 `invite_failed` is `retryable:true` and the same `request_id` may be
  retried. When it cannot be verified, the receipt is `uncertain`, `retryable` is false, and the
  error does not claim the invite was atomic. The device entry stays (harmless, backed up).
- The bus gets `fleet.changed {action,node}` only. The Console's in-memory config gains the device.

Error codes: `bad_body bad_node bad_request_id bad_profile bad_os bad_address bad_label
bad_capabilities bad_acknowledge bad_replace unknown_fields` (400); `main_not_allowed cluster_off
not_main node_enrolled invite_pending already_issued tunnel_forward sshd(retryable) profile_mismatch
invite_failed(retryable) busy(retryable)` (409); `config_unreadable state_unwritable` (500).

### `POST /api/fleet/revoke`  body `{node, confirm}` (`confirm` must equal `node`)
→ `{ok,node,was_enrolled,ssh_key_removed,active_jobs_cancelled,warning?,step:"invite"}`.
Cuts the credential and the SSH key. Main refused (`main_not_allowed`). Idempotent for an
unenrolled node. Frees the node for a fresh invite.

### `POST /api/fleet/capabilities`  body `{node, capabilities:[...], acknowledge?:[...]}`
Replaces the node's capability set through `Broker.set_capabilities`; **the credential is not
rotated** (`credential_rotated:false`). Extra beyond profile default need `acknowledge`.
→ `{ok,node,changed,added,removed,capabilities,note?}`. Errors: `unknown_node` 404 (not in config),
`not_enrolled` 404, `main_not_allowed`. `camera` also needs `jarvis camera allow on` on the node.

## Main transfer (unfinished by design)

### `POST /api/fleet/main-transfer/preview`  body `{target}`
→ `200 {status:"blocked", support:"blocked", mutated:false, target, readiness:[{id,state,why}],
missing:[{id,why}], phases:[str], risks:[str], why}`. `missing` ids: `authority_store
authority_lease fencing target_ack executor_guards fresh_owner_proof checkpoint_barrier`.
Readiness: `known_device not_current_main not_cloud online`. Cloud targets: `not_cloud` problem.

### `POST /api/fleet/main-transfer`
Always `409 {code:"blocked", mutated:false, missing, readiness, phases, risks, target}`. It never
rewrites `cluster.main_device`. A disabled "Make main" button must say it is not built yet.

## Phone

### `GET /phone/api/fleet`
Same shape as `/api/fleet` with `audience:"phone"`: `can_write:false`, `cluster.local:null`, no
addresses, hostnames, invite expiry, code or `code_ok`, and no `locate` capability. `status` may
be `invite_pending` (and the other link states) and `step` text is included. Capabilities are
plain ids. Writes are `blocked` with no `path` on any action, and there is no preview action.
Paired-session auth as every phone route. No phone POST route exists (404). No subprocess, no
probe, no network. The phone projection does not include the invite code.

## Not done (say so in the UI)
- Safe MAIN transfer / handoff: blocked; needs durable authority, lease, fencing, owner proof (P2-P4, P7).
- Fleet-admin for the phone (fresh passkey / `manage_main`).
- Creating cloud servers or installing an OS; any location UI.
- Automatic onboarding of the *node side* (the user runs the shown join command on the node).
- Invite issuance by the CLI (`jarvis cluster invite`) is not covered by the Console's lock/ledger.
- GitHub private/public check and `ahead/behind` freshness need a network fetch: not done in status.
- Nothing committed or pushed to the vault repo; remotes untouched.


Parent review: invitation issuance revalidates on-disk MAIN identity under the mutation
lock, including a role change that lands during the device-entry write. A stale cached MAIN
config cannot issue. That re-check is not a lease and not safe MAIN handoff. Backup names
include nanoseconds to avoid overwriting same-second onboarding backups. Phone capability
projection omits location controls; it does include link status such as `invite_pending` and
the step text, and it omits addresses, hostnames and the code. Cloud enrollment is inventory
by default, is not MAIN-eligible, and does not install a gateway service. The cloud
`main_eligible_why` text does not describe the server as a gateway.


Final parent integration: owner Console revoke/capability mutations now re-read the config
under the management lock as invite already does; stale MAIN role or changed profile cannot
silently authorize a write. This remains config-level protection, not a lease/fence.
Frontend corrections: tunnel requirement direction, failed/pending invite verdicts, archive
backlog labels, phone step actor labels. 31 Node fleet scenarios pass. Integration/restart
status is recorded in IMPLEMENTATION-PROGRESS.md.
