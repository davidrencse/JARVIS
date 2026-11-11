# Grok final verification

Date: 2026-11-11. Read-only except this file. No production Python or JS, config, vault, live broker, SSH key, or remote host was changed. Nothing was committed, pushed, or restarted. No model call. No server. No child agent. The parent log `/tmp/jarvis-grok-gap-integration.log` was read and was not re-run.

`jarvis/continuity/` is still an isolated prototype. Production code does not import it. `jarvis/continuity/__init__.py` says nothing running imports it, no gateway is provisioned, and `cluster.main_device` stays the live authority. That module, and the accepted absence of a MAIN lease, an Oracle/gateway handoff, and a C3 canonical read, are excluded. They are not findings.

This is not a clean bill. The six holes in `AUDIT-INTEGRATED-REPAIRS.md` are closed for the cases the repair docs and the permanent tests actually pin. Five holes remain. A green parent suite does not cover them.

## What this review ran

Parent log, tail only: `Ran 3580 tests in 296.014s`, `OK (skipped=7, expected failures=1)`. The expected failure is `_task_xml round-trips with CR`. The log also prints many `second brain archive drain: drain-failed (failure 1)` lines and two `ResourceWarning`s before that OK. This review did not attribute those lines. They are not treated as a suite failure and they are not treated as proof the holes below are closed.

Focused modules, `JARVIS_NO_MODELS=1`, `/home/jdoe/projects/JARVIS/.venv/bin/python`, cwd the repo:

| Module | Result | What it pins, and what it does not |
| --- | --- | --- |
| `tests.cluster.test_integrated_fleet_gaps` | 13 tests, OK | Settings delta write, invite re-check after authorize, enroll order around the pairing file, grace-boundary claim, Settings/Screen/phone-picker wording. It never calls `jarvis cluster revoke`. It never plants an uncertain receipt during `code_key` on the enroll path. It never interleaves two receipt writers. |
| `tests.data.test_second_brain_overflow` | 17 tests, OK | Contiguous chunk damage: status `error`, rescan plus drain appends a revision, the old chunk stays, validate still lists it. Sibling overflow copy. `_sync_view` maps a reason that starts with `oversize`. It does not delete an earlier revision and then damage the head. |
| `tests.remote.test_lanmouse_guard` | 61 tests, OK | Fresh apart blocks `start`. A stale or missing timestamp is `state` `unknown` and `settled` `apart`, and start is allowed. `sharing_paused` uses raw `apart_now`. It does not call `jarvis lanmouse pair` or `sharedinput.pair`. |

Temp fixtures only, then removed: `/tmp/jarvis-grok-final-verify.py`. `unauthorize`, `code_key`, host key, keypair, sshd, addresses, Lan Mouse `config_path`, status, start, stop, and `local_info` were patched. No live `~/.config/lan-mouse/config.toml`. Wording was checked by reading the JS and by the node helper inside the surface test. No browser.

## Closed claims

| Claim from the repair docs | Verdict | Evidence |
| --- | --- | --- |
| `settings.write` patches only the requested keys under `fleet-manage.lock` and does not write a stale main back | Holds | `jarvis/settings.py:298-330`. `cluster.main_device` still raises at `settings.py:278-279` and writes nothing. Fixture: `json.loads` hook called `cluster_cli.set_main` to `dell` during the voice save. Disk main stayed `dell`, `voice.kokoro_speed` became 1.1, `keep.me` stayed, process memory main stayed `zenbook`. Permanent tests `test_load_hook_set_main_is_not_written_back`, `test_stale_memory_does_not_restore_main`, `test_write_is_busy_while_the_fleet_lock_is_held`, `test_tunnel_is_saved_for_restart_and_main_is_refused`. |
| `jarvis cluster invite` consults the receipt file and re-checks after `code_key`, under the lock, before enroll | Holds | `jarvis/cluster_setup.py:446`, `:528`, `:546-550`. Same injection as the enroll hole below: `code_key` wrote an uncertain receipt, then returned a dummy key. Invite raised `AdmissionBlocked`, `Broker.enroll` was not called, no node row. Permanent test `test_uncertain_receipt_blocks_only_that_node_id` and `test_authorize_tunnel_save_does_not_return_a_code`. |
| CLI enroll rotates the token only after the pairing file, and settles jobs only after that file exists | Holds for that order | `jarvis/cluster_cli.py:239-279`. Four `EnrollOrderTests` OK. The missing second admission check is a different hole (finding 2). |
| At `deadline == now - 15s`, `busy_job` is empty, `claim` marks that running row `uncertain`, takes a different pending job, and does not replay it | Holds | `jarvis/cluster_store.py:213-228`, `:289-324`. Fixture: boundary row became `uncertain`, the pending id was claimed, a second claim returned none, enqueue of the old id stayed `uncertain`, a `done` row stayed `done`. Permanent test `test_exact_grace_boundary_matches_busy_and_is_not_replayed`. |
| Inside the grace window a second direct claim can leave two running rows | Still true, accepted | Reproduced again: two future pending rows, two direct claims, both stayed `running`. Permanent test `test_inside_the_window_stays_running_and_a_second_claim_still_works`. `Hub.dispatch` still refuses only while `busy_job` is set. Not filed again. |
| Settings, Screen hold, and the phone ask picker say busy | Holds for those three strings | `jarvis/web/settings.js:178-186`, `jarvis/web/app.js:701-707`, `jarvis/web/phone/phone.js:648-653`. Permanent surface test OK. Residuals that still use the online bit are listed under accepted limits. |
| A contiguous damaged chunk is not `current`, and rescan plus drain appends a revision | Holds | `test_corrupt_chunk_is_not_current_and_rescan_appends_a_revision` and `RealSize.test_fullsize_corrupt_chunk_is_not_current_and_rescan_repairs`. A missing earlier revision is a different hole (finding 4). |
| Overflow policy is carried into sync, verify, and file sync, and a recreated ledger copies the sibling cap | Holds for those tests | `_archive_brain` at `jarvis/second_brain_runtime.py:165-174`. `test_recreated_ledger_keeps_the_owner_overflow_policy`. CLI `validate` and `restore` still construct `SecondBrain(root)` (`second_brain_runtime.py:571`). Readers still `_load` chunked notes. That is the documented reader behavior. |
| No MAIN lease, no gateway handoff, no C3 canonical read | Accepted, still true | `second_brain_runtime.py:395-396` returns `canonical` false and `lease_fenced` false. Verify sets `canonical` false at `:596`. |

## Findings

### 1. High — `jarvis cluster revoke` leaves the receipt fence up

`jarvis/cluster_cli.py:224-226` calls `cluster_setup.revoke`. That function is `jarvis/cluster_setup.py:593-599`: `Broker.revoke` plus `unauthorize`. It does not open `cluster-invite-receipts.json`.

The only clearer is console `revoke_node` (`jarvis/fleet_management.py:875-902`), which drops every receipt whose `node` is that id and keeps the others.

The fence text tells the operator to revoke. `AdmissionBlocked` (`fleet_management.py:118-128`) says `Revoke {node}`. CLI enroll's unverified-cleanup error (`cluster_cli.py:271-273`) says the same. A successful invite's `next` line (`cluster_setup.py:569-571`) names `jarvis cluster revoke {node}`.

`INTEGRATED-FLEET-GAP-REPAIRS.md` says "Revoke clears every receipt for that id, including a `cli-*` row." That sentence is true of `revoke_node` only. The permanent admission test never calls the CLI revoke.

Observed, temp state, `unauthorize` patched, uncertain receipts planted for `pixel9` and `oracle1`:

- `cluster_cli.run` revoke of `pixel9` returned `{revoked: pixel9, ssh_key_removed: true}`.
- `pixel9` was still `uncertain`. `assert_node_admitted` still raised. The node has no credential and still cannot be enrolled.
- `revoke_node` then cleared `pixel9` and left `oracle1`.

### 2. High — `jarvis cluster enroll` checks admission once, before `code_key`

`jarvis/cluster_cli.py:236-258`. `assert_node_admitted` runs at line 238. `code_key` runs at line 240. `Broker.enroll(..., settle_jobs=False)` runs at line 258. There is no second check and no receipt lock around the enroll.

Invite does the opposite. It checks before `code_key`, again under `config_mutation` (`cluster_setup.py:528`), and again from `_ensure_invite_holds` after authorize and after enroll (`:546-550`). The second refusal is raised before enroll, so enroll is not called.

The window is the duration of `code_key` (an `ssh-keygen` into the state dir) plus an optional device `save_delta`. That is the window the fence exists to close: a credential must not be rotated after an uncertain receipt is written.

Observed, temp config, `code_key` patched to call `note_uncertain_attempt(pixel9)` and then return a dummy `ssh-ed25519` line:

- CLI enroll returned `enrolled: pixel9`.
- The uncertain receipt was already on disk.
- The new token authenticated.
- The same injection on `cluster_setup.invite` raised `AdmissionBlocked` and called enroll zero times (closed claim above).

`tests/cluster/test_integrated_fleet_gaps.py` pins the enroll order around a failed `code_key` and a failed pairing write. It does not pin a receipt that appears during `code_key`.

### 3. High — the receipt file is a full-document replace that ignores the fleet lock

`assert_node_admitted` (`fleet_management.py:690-695`) and `note_uncertain_attempt` (`:698-708`) take neither `_LOCK` nor `config_mutation`. `_store_receipts` (`:648-665`) writes the dict it was given. It does not re-read. `_LOCK` on `issue_invite` / `revoke_node` is a process-local `threading.RLock`. CLI writers do not take it. A console thread that holds `fleet-manage.lock` is therefore not exclusive for this file.

Observed, forced interleaving, temp state (not an unsynchronized flake):

- Thread A held `config_mutation`, loaded the receipts, and waited.
- Thread B called `note_uncertain_attempt(pixel9)` during that wait.
- Thread A stored its stale snapshot, which contained only `dell`.
- The file ended as `[dell]`. The `pixel9` fence was gone while the console still held the lock.

Related, read and not executed as a disk-error fixture: `note_uncertain_attempt`'s callers on the CLI enroll and invite failure paths swallow `OSError` and `ValueError` (`cluster_cli.py:267-270`, `cluster_setup.py:510-513` and `:583-586`). A failed fence write then continues as if the attempt were recorded. That is fail-open on the path whose job is to fail closed.

A successful CLI enroll or invite still writes no `issued` receipt. That part is the documented limit in the repair doc (no `request_id`). `peripherals.act` invite (`jarvis/peripherals.py:104-107`) goes through `cluster_setup.invite`, so it does consult the fence, and it also writes no issued receipt.

### 4. Medium — a gap in the revision list hides a damaged head from status and from rescan

`published_readable` (`jarvis/second_brain.py:1078-1090`) and `publication_sound` (`:1049-1060`) return true when `_latest_info` has `conflict` or `tomb`, and they do not call `_load`. `_record_state` sets `conflict` when the revision numbers are not `1..n` (`:1136-1137`).

Status counts that true as checked and healthy (`jarvis/second_brain_runtime.py:488-502`). `_verdict` (`:478-484`) becomes `current` when `archive_chunk_damaged` is 0. Drain's backlog drop calls `publication_sound` (`jarvis/second_brain_queue.py:831-839`). A true result drops the matching head. `rescan` only seeds (`:538-543`).

`validate` does call `_load` on every revision it still finds, including after it has already recorded `revision-gap-or-duplicate` (`second_brain.py:1336-1349`).

`FINAL-ARCHIVE-REPAIRS.md` says status opens every published head whose note is at most 64 KiB, or that has a `.jrc` beside it, through the same loader, and that one failure makes `state` `error`. The docstring on `published_readable` says tombstones and revision conflicts are true and are not chunk damage. The code matches the docstring. The repair sentence overclaims. A contiguous head, which the overflow tests pin, still goes through `_load`. This hole needs a missing earlier revision.

Tombstones use the same short-circuit. The repair doc says a tombstone stays acknowledged so a damaged chunk cannot block a deletion. That path is not filed again.

Observed, temp state and temp vault, bounds shrunk the same way as the overflow tests (`MAX_RECORD_BYTES` 4096, `CHUNK_BYTES` 1024), one task of 9000 `x` characters, overflow 64 KiB, two chunked revisions (publish, then `store.transition` queued to failed, then drain again). The rev-1 note was deleted. One byte of a rev-2 `.jrc` was flipped.

- Only the rev-2 note remained. `_latest_info` had `conflict` true, rev 2.
- `runtime.status` state was `current`. `archive_chunk_damaged` was 0. `archive_chunk_checked` was 1.
- `rescan` plus drain repaired 0 and left remaining 0. The flipped chunk bytes were unchanged. The note count stayed 1.
- `validate` was not ok: `revision-gap-or-duplicate` and `unreadable: chunked-payload: chunk 9 hash mismatch`.

### 5. Medium — `jarvis lanmouse pair` writes a pairing while a fresh apart reading blocks start

Console and the shared-input action refuse the whole pair. `peripherals.act` (`jarvis/peripherals.py:98-102`) calls `start_blocked` before `pair` and raises the apart sentence. The comment there says pairing restarts a running Lan Mouse.

The CLI does not. `jarvis/__main__.py:362-366` calls `sharedinput.pair` directly. `pair` (`jarvis/sharedinput.py:1246-1270`) exchanges fingerprints and then calls `write_pairing` on both sides. `write_pairing` (`:1202-1213`) always writes the config. `start_blocked` only skips the local daemon restart. The peer `lan_mouse` write still happens.

`start_blocked` (`:1118-1136`): a fresh `state == "apart"` always blocks; unknown does not; `off_when_apart` false blocks nothing. Console JS uses that fresh state (`jarvis/web/peripherals.js:46`, `jarvis/web/sharedinput.js:59`, `jarvis/web/app.js:357`). The 61 guard tests pin start. They do not pin `pair`.

Observed, temp memo with `read_at` equal to the wall clock, `apart_now` true, `off_when_apart` true, daemon status patched to running, `config_path` patched to a temp file, remote and `local_info` patched:

- `start_blocked` returned "Shared mouse and keyboard stays off while the computers are apart. Remote control still works."
- `pair` wrote the temp config with the peer IP.
- Local stop and start were not called (`restarted` false).
- The fake peer `lan_mouse` write was called once.

The next start would use a pairing that was written while the computers read apart. The daemon itself was not restarted in this fresh case.

### 6. Low — three apart readings, three policies

This does not break the written start rule. The tests pin each half. The halves disagree with each other, and a stale memo lets `pair` restart a daemon that the clipboard and the session-end path still treat as apart.

| Reading | `sharing_availability` | `start_blocked` | `sharing_paused` | `ControllerPause._apart` and `_lan_mouse_back` | `pair` / `write_pairing` |
| --- | --- | --- | --- | --- | --- |
| Fresh `apart_now`, age under 300 s | `state` apart, `settled` apart | blocks | "the laptops are apart" | apart | writes the file, does not restart (finding 5) |
| Age over 300 s, or no `read_at`, `apart_now` true | `state` unknown, `settled` apart | None (unknown does not block) | still "the laptops are apart" (`sharedinput.py:1049-1061`, raw `apart_now`) | apart (`sharedinput.py:1008-1012`, `remote_view.py:1291-1300`) | stop plus start of a running daemon |
| `off_when_apart` false | unchanged | blocks nothing | clipboard pause ignores the switch | session end still uses settled/fresh apart | restart follows `start_blocked` |

Observed, same patched pair as finding 5, memo rewritten to `read_at = now - AVAILABILITY_TTL - 30`:

- `start_blocked` was None. Projection `state` was `unknown`, `settled` was `apart`.
- `sharing_paused` was still "the laptops are apart".
- `pair` restarted: local stop 1, local start 1, peer write 1.

`AvailabilityTests` / `StartBlockedTests` encode the start half (`test_unknown_and_together_still_start`, `test_missing_or_stale_timestamp_stays_unknown`). `SharingPausedTests.test_apart_pauses` uses raw `apart_now`. Console `peripherals.act` uses `start_blocked`, so console and CLI agree on the stale start. They do not agree with the clipboard pause or with the remote-control restore.

## Ledger ownership

| Ledger | Owner the repair docs name | Who else writes or ignores it | Diverges |
| --- | --- | --- | --- |
| `state/cluster-invite-receipts.json` | `issue_invite` / `revoke_node` under `_LOCK` and `config_mutation` | CLI `note_uncertain_attempt` and `assert_node_admitted` do not take the lock. `cluster_setup.revoke` never reads the file. Successful CLI invite/enroll and `peripherals` invite write no `issued` row. | Yes. Findings 1–3. |
| `jarvis.config.json` | `patch_config` / `save_delta` under `fleet-manage.lock` | Settings now uses that path for requested keys. Voiceprint threshold is still a separate file under `_WRITE_LOCK` after the config write. Hand editors stay outside the lock. `save_delta` on an empty document (`if not doc`, `config_lock.py:196`) writes the caller's whole document. Empty `{}` is falsy. Not reproduced. | Settings stale-main write-back is closed. The empty-document path and the compare-then-rename window remain. |
| Node token | `Broker.enroll` | Invite re-checks admission under the lock. CLI enroll checks once, then enrolls. | Yes. Finding 2. The pairing-file order itself holds. |
| Job `running` / `uncertain` | `claim`, `get`, `recover`, `settle_rotated` | `running()`, fleet `active_jobs`, and `busy_nodes` do not expire an overdue row. No sweeper. Inside the window a second direct claim is allowed. | Boundary hole is closed. Those read paths stay as documented. |
| Liveness word | `connections.presence` | Settings, Screen hold, and the phone ask picker now read `presence` for the words above. Card class, dot, header count, and `reachable()` still use `online`. Phone fleet `status` stays `link_state`. | Wording hole is closed. Residuals stay. |
| Overflow ceiling and chunk integrity | per-ledger `archive_meta.overflow_max_bytes`; status via `published_readable` | Conflict and tomb skip `_load` and count as healthy. `validate` still `_load`s. CLI validate uses overflow 0 and still reads chunks. `set_overflow` writes ledgers in a loop and keeps going after `sqlite3.Error` (`second_brain_queue.py:578-595`). Partial write was read, not reproduced. | Contiguous damage is closed. Finding 4 remains. |

## Duplicate implementations that can drift

- Revoke: `cluster_setup.revoke` cuts the token and the SSH line. `fleet_management.revoke_node` does that and then deletes that id's receipts. The CLI, the invite `next` line, and the enroll error all name the first one. The fence is cleared only by the second.
- Admission: invite checks before `code_key`, under the lock, and again after authorize and after enroll. CLI enroll checks once, before `code_key`. Receipt replace is a snapshot store with no re-read, and the CLI does not hold the fleet lock while the console does.
- Chunk integrity: `published_readable` / `publication_sound` treat `conflict` as sound. `validate` opens the bytes. Status can say `current` with `archive_chunk_damaged` 0 while validate reports a hash mismatch on the only remaining note.
- Apart: fresh `state`, settled `apart_now`, and raw `apart_now`. Start and the console pair button use the first, with the settled-plus-override exception. Clipboard pause uses the third. Session-end restore uses settled or fresh. CLI `pair` uses `start_blocked` only to skip a restart, after the file write and after the peer write.

## Accepted limits (not filed again)

- No cross-machine MAIN lease, epoch, or fence. The fleet lock is one state directory on one machine. Two config copies can both read as main.
- No Oracle or gateway handoff. Cloud inventory stays inventory-only.
- C3 is not canonical. Verify and status set `canonical` false. `lease_fenced` is false. SQLite remains the source of truth.
- `jarvis/continuity/` is not activated.
- `settle_rotated` after a code or a pairing file exists is best effort. Exceptions are swallowed.
- Hung work stops being `busy` at deadline plus 15 s. An overdue `running` row stays `running` until `claim`, `get`, or `recover`.
- Inside the grace window a second direct `claim` is allowed. Reproduced again this review.
- Records over 64 MiB stay held. Orphan chunks are not collected. The writer is not streaming.
- After a successful contiguous repair, `current` means the acknowledged head read back. `validate` can still list the old revision. That part of `FINAL-ARCHIVE-REPAIRS.md` matches the contiguous tests.
- A plain note larger than 64 KiB with no `.jrc` beside it is not opened by status.
- Phone pairing is not fleet admin. `can_write` on the phone audience stays false. Phone fleet `status` stays `link_state` while `presence` carries busy (`fleet_management.py:357-362`).
- The uncertain fence is the configured node id. A different id may still be issued.
- A device label written before a later invite failure can remain when the credential and key rollback verify.
- Same-thread settings save during an invite is applied. The tunnel change is kept. No code is returned when the re-read fails.
- `jarvis/web/fleet.js:508` still says "I checked. Make a new request for it." and sends `replace_pending`. It does not call revoke. The server returns `recover=revoke`.
- `jarvis/web/remote.js` still says `jarvis cluster invite`. That path now consults the receipt file.
- Connections card class and dot, the Computers header count, and `reachable()` still use `n.online` (`app.js:724-727`). The phone device dot uses `d.online` (`phone.js:376`). The phone meta and screen-reader text do say busy (`phone.js:377-385`). Screen stays disabled while busy (`app.js:701-704`). Those residuals match the repair doc.
- Hand editors, and any writer that skips the lock, stay outside it. The compare-and-swap narrows the window between compare and rename. It does not close it.
- In-process provider replay of a started tool was not re-executed. It stays the accepted P2 limit. Legacy provider subprocess tools stay unmediated.
- `set_overflow` can record `ok` false after an earlier ledger was already updated. Read from `second_brain_queue.py:578-595`. Not reproduced.

## What this run did not do

- Did not launch `unittest discover` or re-run the parent log.
- Did not open a browser, a JARVIS process, SSH, or a real node.
- Did not edit production code, live config, the vault, or the live broker.
- Did not treat the parent `OK` as closing findings 1–5. Those paths have no permanent test.
- Did not restart the console or the hub. The safe-activation sections in the repair docs are operator instructions. They were not followed here.
