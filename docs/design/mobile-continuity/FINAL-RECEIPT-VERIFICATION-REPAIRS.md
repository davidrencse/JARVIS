# Receipt verification repairs

Date: 2026-11-11. Findings 1–3 from `AUDIT-GROK-FINAL-VERIFICATION.md`. Temp fixtures only. No production config, vault, live broker, SSH key, or remote host. No commit, push, CI, service restart, server, or model call. Findings 4–6 (archive revision gap, `jarvis lanmouse pair`, the three apart readings) are not this change. `jarvis/continuity/` was not edited.

The only ledger is still `state/cluster-invite-receipts.json`. There is no second file and no in-memory copy.

## 1. High — CLI revoke clears that node's fence on the console's revoke path

`jarvis cluster revoke` and console `revoke_node` both call `cluster_setup.revoke`. That function holds `config_mutation` (`<state>/fleet-manage.lock`), calls `Broker.revoke`, then `unauthorize`, then `fleet_management.clear_node_receipts`.

`Broker.revoke` runs first. If it raises, `unauthorize` is not called and `clear_node_receipts` is not called. The uncertain rows for that id stay, and so do every other id's rows. `assert_node_admitted` still raises for that id.

`clear_node_receipts` drops every receipt whose `node` is that id, including a `cli-*` row, and leaves every other id. A damaged receipt file raises `FleetError` from the load. Revoke catches that and still returns the credential result, so a damaged ledger does not undo the credential cut and is not treated as an empty ledger.

An `OSError` from `unauthorize` is reported as `ssh_key_removed: false` and the fence is still cleared. The credential is already gone. That matches the console's previous order.

## 2. High — enroll re-checks admission under the lock after key prep

`jarvis cluster enroll` still calls `assert_node_admitted` before `code_key`, so an uncertain row that is already on disk refuses before any signing work.

`code_key`, and the optional new-device `save_delta`, run next. They stay outside the long hold. After both, enroll takes `config_mutation` and calls `assert_node_admitted` again. `Broker.enroll(..., settle_jobs=False)` and the pairing-file write run only after that second check, while the lock is still held. A receipt planted during `code_key`, or during the device delta, raises `AdmissionBlocked` and does not rotate the token.

`settle_rotated` still runs only after the pairing file exists. A verified restore of a failed pairing write still re-raises the original error and writes no uncertain receipt.

## 3. High — one locked delta for every receipt read-modify-write

`fleet_management.mutate_receipts` is the only production read-modify-write of the receipt file. It takes `config_mutation` (re-entrant on the thread that already holds it), reads the file, runs the caller's change on a copy, reads the file again, and writes only that caller's delta via `_merge_receipt_delta`.

A key the caller did not change keeps the value from the second read. A fence stored by a same-thread re-entry during the change is still there when the outer write lands. The caller's earlier full snapshot is not what is written. A key the caller removed is removed only when the second read still has the value they saw. A third value on a key they changed is left as the second read, so a stale pending or issued write does not cover an uncertain row.

`note_uncertain_attempt`, `assert_node_admitted`, `_put_receipt`, `_drop_receipt`, and `clear_node_receipts` all go through that lock. Console `issue_invite` writes pending, uncertain, and issued receipts through `_put_receipt` / `mutate_receipts`. `_store_receipts` is only the atomic bytes write at the end of `mutate_receipts` (and the way tests plant a whole temp file).

Two threads cannot both be between read and write. The second blocks on `fleet-manage.lock` until the first stores. Each fence from both writers is in the file afterwards, including a fence that was already there.

A failed fence write is not treated as recorded. Invite raises `InviteIncomplete` with `retryable` false and says the receipt could not be saved. The console's non-retryable handler then writes that `request_id` as uncertain, which is a second chance on the same file. CLI enroll raises `ValueError` saying the receipt could not be saved, and does not continue into `settle_rotated`. Neither path deletes some other id's fence on the way out.

## Tests

`JARVIS_NO_MODELS=1`, `.venv/bin/python`, temp directories, signing and `unauthorize` patched. `tests.cluster.test_integrated_fleet_gaps`, `tests.cluster.test_config_writers`, `tests.cluster.test_fleet_management`, `tests.cluster.test_cluster_setup`: 132 tests, OK, 13.966s. The parent full suite was not run.

| Test | What it pins |
| --- | --- |
| `EnrollOrderTests.test_receipt_written_during_code_key_does_not_enroll` | `code_key` calls `note_uncertain_attempt(pixel)` and returns a dummy key. CLI enroll raises `AdmissionBlocked`. `Broker.enroll` is not called. No pairing file. The old token still authenticates. The running job stays `running`. The done job stays `done`. The only receipt file is `cluster-invite-receipts.json`. |
| `ReceiptFenceTests.test_receipt_during_device_delta_blocks_before_enroll` | `save_delta` for a new `pixel9` records an uncertain receipt, then performs the real device write. CLI enroll raises `AdmissionBlocked` before `Broker.enroll`. No pairing file. |
| `ReceiptFenceTests.test_cli_revoke_clears_that_fence_and_leaves_the_other` | Uncertain rows for `pixel9` and `oracle1`. `jarvis cluster revoke pixel9` returns `revoked` and `ssh_key_removed`. `pixel9` is admitted. `oracle1` still raises `AdmissionBlocked`. Console `revoke_node` then clears a fresh `cli-pixel9` row and leaves `oracle1`. One receipt file. |
| `ReceiptFenceTests.test_revoke_failure_does_not_clear_the_fence` | `Broker.revoke` raises `RuntimeError`. CLI revoke and console `revoke_node` both leave `pixel9` and `oracle1` uncertain. `unauthorize` is not called. `assert_node_admitted(pixel9)` still raises. |
| `ReceiptFenceTests.test_concurrent_writers_under_a_barrier_keep_every_fence` | A planted `dell` fence. Thread A is inside the locked read, held at a `threading.Barrier`. Thread B reaches a second barrier and calls `note_uncertain_attempt(oracle1)`. B does not finish while A is inside. After both finish, `dell`, `pixel9`, and `oracle1` are uncertain. |
| `ReceiptFenceTests.test_reentrant_writer_does_not_store_a_stale_snapshot` | During `note_uncertain_attempt(pixel9)`, the hash call re-enters and records `oracle1`. The outer store keeps `dell`, `pixel9`, and `oracle1`. |

The earlier admission, enroll-order, settings-lock, and ledger tests in those modules still pass, including `test_uncertain_receipt_blocks_only_that_node_id`, `test_pairing_write_failure_restores_the_token_and_leaves_jobs`, `test_issued_receipt_write_failure_is_not_retried_or_reissued`, and `test_revoke_removes_credential_and_key_and_frees_the_request_id`.

## Remaining limits

- No cross-machine MAIN lease. The fleet lock is one state directory on one machine.
- The uncertain fence is the configured node id. A different id may still be issued.
- A successful CLI enroll or invite still writes no `issued` receipt. The CLI has no `request_id`. Issued-receipt idempotency stays on the console.
- If `Broker.revoke` returns and removing the SSH line raises, the fence for that id is cleared. The credential is already gone.
- If the receipt file cannot be written, the command fails and does not claim the fence was saved. A fence that never landed does not block a later process. The console's second write of that `request_id` is the retry. If that write also fails, a different `request_id` is not fenced. A damaged file is still a hard stop on the next read, not an empty ledger.
- A device row written by the enroll delta can remain when the admission re-check then refuses. The credential is not rotated.
- `settle_rotated` after the pairing file exists is still best effort.
- `save_delta` on an empty document (`if not doc` in `config_lock.py`) still writes the caller's whole document. Empty `{}` is falsy. Not this change.
- Hand editors, and any writer that skips `config_mutation`, stay outside the lock. The config compare-and-swap only narrows the window between compare and rename.
- `_store_receipts` still replaces the file with the dict it is given. Production read-modify-write does not call it with a snapshot from an earlier read. A new caller that does would drop fences it never saw.
- Findings 4–6 remain. `jarvis/web/fleet.js` still sends `replace_pending` and does not call revoke. The server answer is `recover=revoke`.

## Safe activation

Restart the console and the hub on the main so they load this Python. Nodes need the usual code update for anything they run locally. No config, database, or receipt migration. An existing `cluster-invite-receipts.json` is read as it is. The parent runs the full suite after the continuity fixes land together.
