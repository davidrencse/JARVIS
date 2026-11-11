# Integrated repairs audit

Date: 2026-11-11. Read-only except this file. No production Python or JS, config, vault, live DB, SSH keys, or remote host was changed. Nothing was committed or restarted. No model call. No second full suite. The parent log `/tmp/jarvis-integrated-final-repairs.log` was not read and was not re-run.

Evidence is `/tmp/jarvis-integrated-repairs-audit.py` (JSON: `/tmp/jarvis-integrated-repairs-audit.json`), run with `JARVIS_NO_MODELS=1` and `/home/jdoe/projects/JARVIS/.venv/bin/python` against temp directories only. The archive case published one real chunked note (canonical payload 16,781,786 bytes) and flipped one byte of a `.jrc`. Settings.js `machineStatus` was executed with Node on the extracted function, not in a browser. Line numbers are from this tree after those runs.

`jarvis/continuity/` is an isolated prototype. Only its own tests import it. It is excluded from acceptance and is not a finding.

This is not a clean bill. Several focused tests in the tree pass and still do not cover the holes below. A green `test_config_writers` or overflow suite does not close them.

## Still closed, or a documented limit that is still true

| Claim | Now | Evidence |
| --- | --- | --- |
| Settings can set `cluster.main_device` | Closed | `settings.write` raised "Moving MAIN is unavailable until safe handoff is implemented." Disk was not changed by that call. The leftover `[data-make-main]` handler in `jarvis/web/settings.js:276-277` only toasts. |
| In-memory invite ledger | Closed | No `_LEDGER` symbol remains. The console ledger is `state/cluster-invite-receipts.json`. |
| Console new `request_id` for a node that already has an uncertain receipt | Closed on that path | `issue_invite` returned `invite_failed`, `retryable` false, `recover` `revoke`. |
| `jarvis archive overflow` / `--mib` unwired | Stale doc | `FINAL-ARCHIVE-REPAIRS.md` still says `__main__.py` has no overflow action. `jarvis/__main__.py:180-187` registers `overflow` and `--mib`. |
| Damaged chunk is not re-queued by rescan | Still true | Reproduced. See finding 6. The repair note in `FINAL-ARCHIVE-REPAIRS.md` is accurate and incomplete: status can still say `current`. |
| `settings.write` is outside the fleet lock | Still true, and stronger than the compare-and-swap sentence | `config_lock.py:15-18` and `FINAL-FLEET-REPAIRS.md` describe a window between compare and rename. The repro is a whole-file stale save, and a save that does not wait on the lock at all. Finding 1. |
| No MAIN lease, no Oracle/gateway handoff, no C3 canonical read | Accepted, not filed | Two config copies can still both read as main. Personal SQLite stays the source of truth. |

Provider replay (P2) was read, not re-executed. `Failover.ask` (`jarvis/inference.py:338-348`) still refuses the stand-in when a tool started (`tool_log` or `request.tools`). No new in-process replay hole is filed. Legacy provider subprocess tools stay unmediated. That is the accepted limit in `P2-REPLAY-FIX.md`, not a lease.

## Findings

### 1. High — `settings.write` rewrites the whole config and does not take the fleet lock

`jarvis/settings.py:254-294`. `jarvis/config_lock.py:15-18`. `jarvis/cluster_cli.py:67-84`.

`write` rejects `cluster.main_device`, then under `_WRITE_LOCK` only it `json.loads` the whole file, patches every requested key onto that document, and `save_json`s the whole document. It never opens `fleet-manage.lock`. Cluster keys are written to disk and skipped in the in-memory config (`settings.py:287-288`), so the running process keeps the old tunnel until restart.

Observed, temp config:

- A voice save whose `json.loads` ran `cluster_cli.set_main(..., "dell")` and then returned the stale document put `cluster.main_device` back to `zenbook`. `voice.kokoro_speed` became 1.1. `keep.me` survived. The API never accepted a main change. The managed write was undone by an unrelated save.
- While another thread held `config_mutation`, `settings.write` of `cluster.tunnel=reverse` returned normally (`wrote`, not `ConfigBusy`). Disk tunnel was `reverse`. The in-memory config stayed `forward`.

The same writer inside an invite: `authorize` runs after `_role_guard` (`jarvis/cluster_setup.py:490-509`). A `settings.write` of `cluster.tunnel=reverse` from that callback still let `issue_invite` return a `jarvis-invite-1:` code. Disk tunnel was `reverse`, the device row and the node row were kept. The role guard had already passed. The next invite that re-reads the file will refuse a reverse tunnel, but this code was already issued.

### 2. High — an uncertain receipt does not own invites

Console rule: `jarvis/fleet_management.py:722-728`. Any uncertain receipt for that node rejects every new `request_id` with `recover=revoke`.

That file is not read by `jarvis cluster invite` (`jarvis/cluster_cli.py:214-216` calls `cluster_setup.invite`) or by `cluster_setup.invite` (`jarvis/cluster_setup.py:436-467`).

Observed, one temp state, one planted uncertain receipt for `oracle1` / `req-old-uncertain`:

- Console `req-new-same-node` with `replace_pending` true: `invite_failed`, not retryable, `recover` `revoke`.
- Console `pixel9` (a different node id): a code was returned.
- `cluster_setup.invite(..., "oracle1")` then also returned a code. The uncertain receipt was still `uncertain`.

So the fence is per node id, and only on the console path. Renaming the computer id in the form escapes it. The CLI enrolls the same id anyway. Both credentials can exist. Revoke is the only console reconciliation, and the CLI never consults it.

The console checkbox does not call revoke. `jarvis/web/fleet.js:507-508` labels it "I checked. Make a new request for it." `doIssue` (`fleet.js:605-622`) mints a new id, sends `replace_pending`, and on a non-retryable `invite_failed` stores that new id as `uncertain.req`. The server message (`errText`, `fleet.js:85-88`) will say to revoke. The checkbox and the server rule do not compose. This UI path was read, not clicked.

`jarvis/web/remote.js:135` still tells the operator to pair with `jarvis cluster invite`, which is the path that skips the receipt file. `index.html:374` correctly points at Connections. `peripherals.js:108` says `jarvis cluster join`, which is the node side.

### 3. High — `jarvis cluster enroll` settles jobs before a pairing file exists

`jarvis/cluster_cli.py:244-250`. `Broker.enroll` defaults `settle_jobs=True` (`jarvis/cluster_store.py:100-122`) and marks running jobs `uncertain` inside the same transaction that replaces the token. `save_json` of the bundle, which calls `code_key(state)`, happens after that.

The invite path was fixed the other way around: `settle_jobs=False`, then `settle_rotated` only after the code exists (`jarvis/cluster_setup.py:509-527`). That later call swallows every exception. If it fails, the credential is already dead and the job stays `running` until `get()` or a hub `recover()`. That swallow was not re-raised in this run. It is the documented best-effort limit, and it is a different order from the CLI.

Observed, temp broker, node `pixel` already enrolled, one running job, `code_key` raised `ssh-keygen failed`, output path did not exist beforehand:

- The exception propagated.
- The job state was `uncertain`.
- `authenticate(old_token)` was false.
- The pairing file was absent, so the new token was not saved.
- The node row was still there.

The node is enrolled to a token that was discarded, and work it had already claimed will not be redelivered.

### 4. Medium — an overdue running job is invisible to "busy", and a second job can be claimed

`connections.presence` (`jarvis/connections.py:18-28`) says online only inside `ONLINE` (75 s). Busy is a running job whose deadline is still inside `GRACE` (15 s) (`busy_nodes` at `connections.py:31-46`, `Broker.busy_job` at `cluster_store.py:203-217`). The row stays `state='running'` until `Broker.get` (`cluster_store.py:341-360`) or `Broker.recover` (`cluster_store.py:270-276`). `claim` (`cluster_store.py:278-307`) does not look for another running row. `Broker.running` (`cluster_store.py:197-201`) and fleet `active_jobs` (`fleet_management.py:220-221`) count every `running` row, deadline ignored. `Hub.dispatch` (`jarvis/cluster.py:629-638`) refuses only when `busy_job` is set. Dispatch itself was not started.

Observed, temp broker, `seen` and `deadline` both 100 s in the past:

- `busy_job` was null, `busy_nodes` was empty, `presence(seen, busy=False)` was `offline`.
- The first row was still `running`.
- A second `enqueue` + `claim` succeeded. Two rows were `running` at once.

`connections.py:22` says a node runs one job at a time and does not poll meanwhile. That is true only inside the busy window. After the deadline plus grace, the hub will queue more work while the first row is still running. This is not the missing lease. It is the single-job rule and the busy flag disagreeing.

### 5. Medium — a live busy node is still "Offline" on Settings, Screen, and the phone picker

Separate temp broker: `seen` 100 s old, running job still inside its deadline.

- `busy_nodes` was `["pixel"]`, `busy_job` was set, `presence` was `busy`.
- `settings.machines` returned `online` false, `presence` `busy`, `paired` true (`jarvis/settings.py:230-233`).

Node execution of `machineStatus` (`jarvis/web/settings.js:178-183`) on that object returned tone `down` and the line "Offline · last seen 1m". The function returns on `!m.online` and never reads `m.presence`. `FINAL-SURFACE-REPAIRS.md` says the parent follow-up superseded this and that "Settings exposes the shared presence." The Python field is exposed. The renderer still drops it.

Same split on other surfaces, from the current source (not a browser pass):

- `jarvis/web/app.js:689-706` uses `presence` for the pill text, and `n.online` for the card class, the dot, and the Screen button. `reachable` (`app.js:829-833`) is online-only, so Screen stays disabled while the node is busy. The Computers header (`app.js:718`) counts `n.online` only.
- `jarvis/web/phone/phone.js:377` and `:551` say "busy, awaiting contact" from `d.presence`. The device dot (`phone.js:376`) and the ask-target option (`phone.js:655`) use `d.online` and append " (offline)" whenever it is false, including busy.
- Owner fleet `status` becomes the presence word. Phone fleet `status` stays `link_state` (`fleet_management.py:339-346`), which has no busy word, while `presence` carries busy. On the two-job broker, owner `status` was `busy` and phone `status` was `offline`, with `presence` `busy` on both. The phone text that reads `presence` matches the contract. The picker does not.

`link_state` (`fleet_management.py:241-251`) is still the enrollment word on purpose. `Hub.alive` (`cluster.py:577-592`) treats `busy_job` as ready so dispatch does not fall back to SSH. Those are different questions and should stay different. The bug is surfaces that ask "is it offline?" by reading `online` after presence exists.

### 6. High — archive status says `current` while a chunk is corrupt, and the supported sync cannot repair it

`jarvis/second_brain_queue.py:129-138`, `:229-237`, `:472-488`. `jarvis/second_brain.py:954-997`, `:1014-1023`. `jarvis/second_brain_runtime.py:436-459`, `:463-468`, `:504-517`.

Drain and capture read `archive_meta.overflow_max_bytes` per ledger. `sync`, `parity`, `verify`, `file_sync`, and CLI sync construct `SecondBrain(root)`, whose overflow is 0 (`second_brain_runtime.py:169`, `:517`, `:547`). Readers still accept a chunked note up to 64 MiB. `rescan` inserts source keys into `archive_backfill`. `_enqueue` returns 0 when `payload_sha` already matches `archive_head`, and `_backfill` then deletes the backlog row. Nothing opens the chunk.

Observed, temp state and temp vault, overflow policy 32 MiB, one task whose canonical payload was 16,781,786 bytes:

- First drain: `published` 1, `chunked` 1, `remaining` 0. Five `.jrc` files.
- One byte of the first `.jrc` was flipped.
- `rescan` reported `tasks` seeded 1. Backfill count was 1. Outbox count was 0.
- The following drain: `published` 0, `unchanged` 0, `remaining` 0. The head row was still set. The outbox was empty.
- `SecondBrain(vault).validate()` was not ok: `unreadable: chunked-payload: chunk 4 hash mismatch`.
- `runtime.status` gate was `ok` and `state` was `current`. Held 0, pending 0, `archive_overflow_max_bytes` 33,554,432. `_verdict` never calls `validate` and never reads a chunk (`second_brain_runtime.py:436-458`).
- `SecondBrain(vault)` (overflow 0) parity `in_sync` false, tasks `skipped_other` 1. Dry-run sync wrote 0 and skipped `oversize: 16781786 bytes`. `_sync_view` counted that as `other`, because it keeps only the exact strings `secret-like` and `oversize` (`second_brain_runtime.py:467`).
- `SecondBrain(vault, max_overflow_bytes=32 MiB)` dry-run sync: `written` 1, no skip, no failure. `repaired` stayed `[]` because the dry-run returns before that list is filled (`second_brain.py:976-978`). The repair path is the one that would append a revision. It was not applied.
- CLI `sync --apply` while capture is on returns `capture-active-use-drain` (`second_brain_runtime.py:510-513`). The command the operator is told to use cannot perform that repair.

`jarvis archive verify` is `validate` plus `parity` on `SecondBrain(root)` (`second_brain_runtime.py:543-555`). Those two calls already failed on this vault. `status` does not call them, so the word `current` and the verify result disagree.

A second policy split, same temp state: `set_overflow` had written 33,554,432 into memory, history, and tasks. Deleting `history.sqlite3` and constructing `History(state)` adopted capture (`second_brain_queue.py:159-168`) without copying `overflow_max_bytes`. History's meta value was absent. Memory and tasks stayed 33,554,432. `queue.status` `overflow_max_bytes` stayed 33,554,432, because status keeps the max (`second_brain_queue.py:455-456`). History command text is truncated to 500 characters, so this one store cannot reach the bound. `adopt` is shared. A memory or tasks file recreated the same way would drain at overflow 0 while status still showed 32 MiB.

Orphan chunks and payloads over 64 MiB held forever remain the documented limits. They were not re-tested.

## Ledger ownership

| Ledger | Who is supposed to own it | Who else writes or ignores it | Diverges |
| --- | --- | --- | --- |
| Invite receipt file `cluster-invite-receipts.json` | `issue_invite` / `revoke_node` | `cluster_setup.invite`, `jarvis cluster invite`, `jarvis cluster enroll` never read it | Yes. Finding 2. |
| `jarvis.config.json` | `patch_config` / `save_delta` under `fleet-manage.lock` | `settings.write` full-document replace; any editor | Yes. Finding 1. |
| Node token (`nodes.token_hash`) | `Broker.enroll` | Invite rotates with `settle_jobs=False` and settles jobs only after the code. CLI enroll settles inside `enroll`, then can fail before the bundle is written | Yes. Finding 3. |
| Job `running` / `uncertain` | `settle_rotated`, `enroll(settle_jobs=True)`, `get`, `recover` | `claim` and `dispatch` consult `busy_job` only. Overdue `running` rows stay until polled or hub restart | Yes. Finding 4. |
| Liveness word | `connections.presence` | `link_state`, `Broker.nodes().online`, `Hub.alive`, Settings renderer, `app.js` `reachable`, phone picker | Yes. Finding 5. |
| Overflow ceiling | `archive_meta.overflow_max_bytes` on each ledger | `status()` publishes the max. Sync, parity, verify, and file sync use 0. `adopt()` does not copy the key. Readers accept chunks up to 64 MiB anyway | Yes. Finding 6. |

## Duplicate implementations that can drift

- Config mutation: `patch_config` changes one key on a re-read document. `settings.write` validates against the process config, then saves a full snapshot it read earlier. `cluster_cli.set_main` uses the lock. A settings save can put `main_device` back without ever sending that field.
- Invite authority: the receipt file, `link_state` (enrolled / invite_pending), and the SSH key line. The console checks the receipt and then `link_state`. The CLI checks neither receipts nor the console's uncertain rule.
- Job uncertainty: invite settles after the code; CLI enroll settles before the file; `get` settles one overdue row when someone polls it; `recover` settles every running row on hub restart. Nothing settles an overdue row just because `busy_job` has gone null.
- Busy versus online: `presence`, `busy_nodes` / `busy_job`, `Broker.running`, fleet owner `status`, fleet phone `status`, `Hub.alive`, Settings `machineStatus`, Connections `nodeCard` / `reachable`, phone text versus phone dot and picker. The Python projection is one function. The renderers are not.
- Overflow: per-ledger meta, status max, drainer `SecondBrain(max_overflow_bytes=_overflow)`, CLI/sync/parity/verify `SecondBrain(root)`, `_verdict` (queue only), `_sync_view` (exact reason string). A corrupt chunk is `current` in status, `other` in CLI sync, and a real hash error in `validate`.

## Accepted gaps (not regressions)

- No cross-machine MAIN lease, epoch, or fence. The fleet lock is one state directory on one machine.
- No Oracle or gateway handoff. Cloud profile stays inventory-only.
- C3 is not a canonical read. `verify` sets `canonical` false. SQLite remains the source of truth.
- `settle_rotated` after a successful invite is best effort. A failure there leaves a dead credential and a `running` job until deadline handling or hub restart.
- Hung work stops being `busy` at deadline plus 15 s. Finding 4 is the extra fact that the row stays `running` and another job can be claimed. The grace itself is intended.
- Records over 64 MiB stay held. Orphan chunks are not collected.
- Phone pairing is not fleet admin. `can_write` on the phone audience stays false. Not re-tested here.
- In-process provider replay of a started tool is blocked. Unmediated legacy provider subprocesses are not a new hole.

## What this run did not do

- Did not start or wait on `/tmp/jarvis-integrated-final-repairs.log`.
- Did not run `unittest` discovery, a browser, or a JARVIS process.
- Did not call `Hub.dispatch`, SSH, or a real node.
- Did not re-run the P2 replay tests. The replay conclusion is from reading `jarvis/inference.py` and `jarvis/chatgpt.py`.
- Did not treat `jarvis/continuity/` as part of the integrated repair.
