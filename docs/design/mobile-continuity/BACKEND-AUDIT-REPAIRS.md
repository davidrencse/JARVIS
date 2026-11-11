# Backend audit repairs (2026-11-11)

Scope: the five reproduced findings in `AUDIT-FLEET-BACKEND.md`. Production code is **not
activated**. The JARVIS Python process must be restarted before these modules load. Nothing
was restarted. No live `.jarvis` state, vault, invite, revoke, or SSH key was written.
MAIN handoff stays blocked (`POST /api/fleet/main-transfer` still returns 409 `blocked`).
This is not a lease and not a fence.

Owned files: `jarvis/chatgpt.py`, `jarvis/inference.py`, `jarvis/assistant.py` (per-turn
logs only), `jarvis/fleet_management.py`, `jarvis/cluster_setup.py` (invite compensation and
the caller's receipt), `jarvis/cluster_cli.py` (`set_main` lock only),
`docs/design/mobile-continuity/FLEET-API.md`, and the tests named below.
`jarvis/ui.py`, `jarvis/web/*`, `jarvis/second_brain.py`, memory, history, store, vault, and
archive tests were not edited.

## 1. Shared successful-tool log

**Cause.** One lane list, `tool_log`, was shared. After turn A returned, turn B cleared that
list. The orchestrator then read the shared list, lost A's finished tool, and the fallback
ran it again. A nudge merge could also prepend into a list another turn had rebound.

**Fix.**

- `ChatGPTLane` (`jarvis/chatgpt.py`): under the lane lock, `_remember_turn` stores the list
  object and a tuple snapshot on thread-local state. `turn_log()` returns a copy, or `None`
  if this thread has not finished a turn. An empty list is a real snapshot. `ask` clears the
  snapshot at entry. The usage-limit fast path sets `exc.tool_log = []` and does not touch
  the shared list.
- `Brain`: `threading.local` publishes a tuple. `tool_log` prefers an explicit failed-turn
  log, then this thread's snapshot, then `last.tool_log`. A lane that has `turn_log` is never
  read from the shared list. The first-turn merge prepends only onto `owned_log()` while that
  object is still `lane.tool_log`. A rebound list is left alone. A lane with no `turn_log`
  (ToolLane) still prepends onto its current list. A nudge snapshots the second turn before
  the merge and publishes `earlier + second`.
- `Failover` (`jarvis/inference.py`): on success, after the inner ask returns, stores
  `tuple(_capture())`. `_capture` uses `turn_log()` when the lane has one. The `tool_log`
  property still follows `last`. An empty snapshot does not invent a tool. Started-but-unfinished
  tools stay on `exc.tool_unfinished` and still block replay. A lane with no `turn_log`
  (FastLane) still copies `tool_log`.
- `assistant._request_tools`: the request snapshot when `turn_log()` is not `None`, including
  an empty list. An exception that already has `tool_log` (including empty) still wins, so a
  failure before any tool still fails over.

## 2. MAIN config flip during the device write

**Cause.** `issue_invite` checked MAIN, then `_write_device` wrote the device entry, then
`invite` used the config object from before the write. A `main_device` change inside that
write still issued.

**Fix** (`jarvis/fleet_management.py`): after `_write_device`, re-read the file, `_require_main`,
check tunnel and sshd on that fresh config, and pass that object to `cluster_setup.invite`.
If the new read is not MAIN, the result is `not_main` and invite is not called. The device
entry may already be on disk; that leftover is harmless and backed up.

`jarvis cluster main` (`cluster_cli.set_main`) takes `state/fleet-manage.lock` when `state`
is passed, re-reads the file, and writes only `main_device` onto that fresh file so a device
an invite just added is kept. Callers that pass no `state` still write the in-memory config
with no lock. `jarvis cluster configure` and a hand-edit do not take the lock. The CLI
`jarvis cluster invite` does not take it either.

The re-read is a config check on this machine. It does not fence another machine, and a role
change after the re-read is still unseen. Safe MAIN handoff is not implemented.

## 3. Keys before side effects, owned rollback

**Cause.** `cluster_setup.invite` authorized and enrolled before `code_key` / `host_key`. A
`RuntimeError` from `code_key` escaped as an uncaught 500. A later failure could leave a new
credential, and a naive rollback could delete a credential that was already there.

**Fix** (`jarvis/cluster_setup.py`): output-exists, not-main, tunnel, sshd, and address checks
still raise `ValueError` before any key or credential work. Then `code_key(state)`,
`host_key()`, and `keypair(node)`. Then snapshot the node row and the existing
`jarvis-node:{node}` lines. Then device write (if needed), `authorize`, `Broker.enroll`,
`encode` (the signing key and host key already fetched; `code_key` is not called again), and
the optional output file.

`InviteIncomplete` is a `RuntimeError` with `.retryable`. `issue_invite` catches it before
the general `RuntimeError` handler. Compensation:

- `_restore_node` deletes or restores the row only while `sha256(token)` still owns it. If
  enroll never returned a token, the row must still equal the prior snapshot.
- `_restore_key` removes only lines that end with ` jarvis-node:{node}` and contain this
  attempt's `type key` bits, then puts the prior lines back. A foreign line for this node
  is not deleted.
- Both restores true → `retryable: true` and 409 `invite_failed`. Either false → the message
  says the invite was not atomic and retrying this request would not be safe, `retryable`
  is false, and the receipt is stored as `uncertain`. The public message does not include
  the private key or the invite code; those stay on `__cause__`.
- `code_key` failing before the try is a 409 `invite_failed` with `retryable: true` (HTTP
  included). No authorize, no row.
- An existing credential is not deleted. `Broker.revoke` is not used for rollback.

## 4. Durable issuance receipt

**Cause.** The issuance ledger was an in-process `OrderedDict`. Clearing it (process restart)
let the same `request_id` plus `replace_pending: true` enroll a second code while the sqlite
invite was still there.

**Fix** (`jarvis/fleet_management.py`): `state/cluster-invite-receipts.json`, version 1.
Fields per id: `node`, `state` (`pending` | `issued` | `uncertain`), `at`, and `digest` only
when issued. Digest is sha256 of `request_id\nnode\nissued`. No code, token, or private key.
Write is `atomic_write` mode `0o600` with fsync. Directory fsync is best-effort. A missing
file is empty. A file that cannot be read, is the wrong shape, or contains a secret mark is
409 `invite_failed` with `retryable: false`. It is not treated as empty.

Under the existing mutation lock, before the device write: store `pending`. After a
successful invite, store `issued` plus the digest, then the in-memory cache, then return the
code. If that issued-store fails, the pending receipt stays, the code is not returned, and
the error is `retryable: false`.

Same `request_id` after restart, including `replace_pending: true`: `already_issued` when the
receipt is `issued`, or when it is `pending` and the node row exists. A new id is required
to replace. `uncertain` is `invite_failed` and is not retried. A `pending` receipt with no
credential row may retry (a clean failure deletes that pending receipt). The in-memory
ledger is a cache. `LEDGER_MAX` does not drop the file. Revoke drops receipts for that node
after the credential is cut. A damaged receipt file does not block revoke.

A sqlite invite that predates receipts has no `request_id`. A **new** id plus
`replace_pending` can still rotate that legacy row. That hole is unchanged.

The filename matches the existing `cluster-*` archive exclusion in `jarvis/second_brain.py`.
That file was not edited.

## 5. Cloud copy and the phone projection

Cloud `main_eligible` stays false. `main_eligible_why` for a cloud profile is
"Inventory only by default and never becomes the main." The profile `about` still says a
cloud server does not install a gateway service. Enrollment does not install a gateway.

Phone behavior was not changed. `FLEET-API.md` now matches the projection: `status` may be
`invite_pending` and `step` text is included. The phone response still omits addresses,
hostnames, invite expiry, the code, `code_ok`, `locate`, and write paths. `can_write` is
false. There is no phone POST route.

## Tests

These assert the fixed behavior. Temp directories and mocks only.

| Test | What it pins |
|---|---|
| `AssistantNeverReplaysAnAmbiguousEffect.test_two_threads_keep_a_finished_tool_when_the_shared_log_is_cleared` | Two threads, barrier. B clears the shared log after A returns. Orchestrator still reports `open_app` partial and does not replay. |
| `NudgeMergeLeavesOtherListsAlone.test_failover_publishes_both_turns_without_writing_into_another_list` | First + nudge turns publish both tools. The other list is unchanged. |
| `InviteTests.test_main_flipped_during_device_write_does_not_invite` | Flip `main_device` inside `_write_device`. `not_main`, no invite, no sqlite row. |
| `InviteTests.test_tunnel_flipped_during_device_write_does_not_invite` | Same wrapper, tunnel becomes reverse. `tunnel_forward`, no invite. |
| `InviteTests.test_restart_cannot_reissue_the_same_request_even_with_replace_pending` | Clear the memory ledger. Same id plus `replace_pending` is `already_issued`. Receipt has no code. A new id rotates. |
| `InviteTests.test_pending_receipt_with_a_row_is_not_rotated` | Pending receipt and a credential row. Same id does not enroll again. |
| `InviteTests.test_pending_receipt_without_a_credential_can_retry` | Pending receipt, no row. Same id invites. |
| `InviteTests.test_damaged_receipt_file_is_not_treated_as_empty` | A `{` file is `invite_failed`, `retryable` false, no invite. |
| `InviteTests.test_uncertain_cleanup_is_not_retried_and_does_not_claim_success` | Unverified cleanup is not retried and does not claim success. |
| `InviteTests.test_cloud_why_is_inventory_not_a_gateway` | Cloud why text has no `gateway`. `main_eligible` is false. Profile about still says no gateway service. |
| `RevokeAndCapabilityTests.test_revoke_removes_credential_and_key_and_frees_the_request_id` | Also: after revoke, the same id can pair again once the receipt is dropped. |
| `SigningKeyAndRoleLockTests.test_code_key_failure_is_retryable_and_leaves_no_credential` | `code_key` `RuntimeError` before authorize. 409 `invite_failed`, retryable, no row. A second call is still a retry. |
| `SigningKeyAndRoleLockTests.test_code_key_failure_over_http_is_invite_failed` | Same failure over HTTP is 409 `invite_failed`. Body has no private key. |
| `SigningKeyAndRoleLockTests.test_set_main_with_state_waits_on_the_fleet_lock_and_keeps_new_devices` | Held fleet lock → `busy`. After release, re-read keeps a device the stale config lacked. |
| `InviteRollbackTests.test_signing_key_failure_happens_before_authorize` | No authorize, no enroll, previous key line kept. |
| `InviteRollbackTests.test_authorize_failure_keeps_the_previous_line_and_does_not_enroll` | Retryable. File unchanged. No enroll. |
| `InviteRollbackTests.test_enroll_failure_removes_only_the_new_key_line` | New `jarvis-node` line removed. Previous line kept. |
| `InviteRollbackTests.test_encode_failure_on_a_new_node_leaves_no_credential` | New node: no row, previous key line kept. |
| `InviteRollbackTests.test_encode_failure_while_replacing_restores_the_old_credential_and_key` | Old token authenticates. New key line gone. Prior line back. |
| `InviteRollbackTests.test_unverified_cleanup_does_not_delete_a_row_it_no_longer_owns` | Hash no longer ours. Row kept. `retryable` false. Public message has no private key. |

Existing regressions in the same modules stayed green, including pre-tool failover, the
usage-limit path leaving `tool_log` alone, ToolLane first-turn blame, and phone
`invite_pending`.

**Focused run (this agent):**

```
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest \
  tests.cluster.test_fleet_management tests.cluster.test_cluster_setup \
  tests.cluster.test_cluster_roles tests.mobile.test_phone_fleet \
  tests.intelligence.test_inference_started_tools \
  tests.intelligence.test_chatgpt_started_tools \
  tests.intelligence.test_inference_races tests.intelligence.test_inference \
  tests.audits.test_audit_brain tests.audits.test_audit4_brain \
  tests.desktop.test_arch_control tests.desktop.test_actions.AuditTwoLaneTests \
  tests.runtime.test_harness_baseline -q
```

`Ran 230 tests` / `OK` (13.2s). The full suite was not run. The known oversize-HTTP
`BrokenPipe` (about 1 in 20 of the 3334-test suite) was not touched. Stderr from
`test_unexpected_exceptions_do_not_leak` prints a `RuntimeError` traceback on purpose; that
test passes.

## Migration

No sqlite schema change. The next invite creates `state/cluster-invite-receipts.json` in the
state folder (mode `0o600`). Invites issued before that file exists have no receipt. A new
`request_id` with `replace_pending` can still rotate one of those pending rows. After the
first receipt, the same id cannot issue again across a process restart.

`cluster-invite-receipts.json` already matches the `cluster-*` exclusion, so it is not an
archive source. `jarvis/second_brain.py` was not changed.

## Restart

Restart the JARVIS Python process to load `chatgpt.py`, `inference.py`, `assistant.py`,
`fleet_management.py`, `cluster_setup.py`, and `cluster_cli.py`. This session did not restart
it, did not write the live state folder, and did not activate fleet, phone, or handoff.

## Remaining risk

- The post-write re-read is a config check. It is not a lease. A `main_device` change after
  that read, on another machine, or by `jarvis cluster configure` / a hand-edit, is unseen.
  `set_main` without `state` still writes the in-memory config unlocked.
- `jarvis cluster invite` (CLI) does not take `fleet-manage.lock` and does not write a receipt.
- A crash after the credential commit and before the HTTP response leaves a pending receipt
  plus a row. The same id will not mint a second code. The code itself is lost.
- `Broker.enroll` marks running jobs uncertain before encode. A failed replace of a live CLI
  invite can leave those jobs uncertain even when the token is restored. Rollback does not
  call `Broker.revoke`.
- The receipt file has no cap. Evicting the in-memory ledger does not drop issued ids.
- Directory fsync failure is ignored after the file fsync.
- A legacy sqlite invite with no receipt can still be rotated by a new `request_id` and
  `replace_pending`.
- FastLane has no `turn_log`. Failover copies its `tool_log` after the ask returns.
- Phone UI was not opened in a browser. Phone projection code was not changed; the API doc
  was corrected to match the existing tests.
