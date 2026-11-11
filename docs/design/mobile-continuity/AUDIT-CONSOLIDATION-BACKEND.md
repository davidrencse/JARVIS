# Backend consolidation audit

Date: 2026-11-11. Read-only. No production code, tests, config, live DB, or vault was edited. The parent suite at `/tmp/jarvis-repair-battery-combined.log` was not re-run. Live vault parity (832 records) was not re-checked. Dell battery hardware was not read.

Evidence is `/tmp/jarvis-consolidation-backend-audit.py` (JSON: `/tmp/jarvis-consolidation-backend-audit.json`), plus two short follow-ups in this session. All of it uses temp directories, `JARVIS_NO_MODELS=1`, and `/home/jdoe/projects/JARVIS/.venv/bin/python`. Line numbers are from the tree after those runs.

This is a same-machine logic review. Missing cross-machine MAIN leases, gateway service, safe handoff, and distributed compute are unimplemented on purpose and are not findings. Obsidian SQLite-primary (C3) is not done. Personal SQLite files stay the source of truth.

## Old findings

| Prior finding | Now | Evidence |
| --- | --- | --- |
| Fleet 1. A finished tool step can be forgotten, or one turn can write into another turn's tool list | Resolved | Two threads on one `Brain`. After B's ask replaced `tool_log`, A's `assistant._request_tools` was still `[("A", true, "ok")]` and B's was `[("B", true, "ok")]`. A rebound `tool_log` was left unchanged by `_merge_first_turn`. |
| Fleet 2. A MAIN change during the locked invite still issues a credential | Changed, still open | A flip that lands before `_write_device` reads is refused (`not_main`, 0 invite calls, 0 node rows, disk main stays `dell`). Two later windows still issue. Findings 1 and 2. |
| Fleet 3. The credential is stored before the code exists, and that failure is not retryable | Resolved for the signing key. Job remainder still open | `code_key` raises before `authorize`. Two tries, both `invite_failed` retryable, 0 node rows, authorized_keys unchanged, `authorize` never called. A failed re-invite still leaves running jobs `uncertain`. Finding 4. |
| Fleet 4. The same request id issues a second code after the in-memory ledger is cleared | Resolved | Issue once, `fm._LEDGER.clear()`, same id with `replace_pending` returns `already_issued`. One invite call. The receipt file has the id and no invite code. |
| Fleet 5. The fleet payload calls a cloud node a gateway | Resolved | Phone inventory why-text is "Inventory only by default and never becomes the main." The profile about-text says enrollment does not install a gateway service. `can_write` is false and no write action is `supported`. |
| Obsidian 1. Arming capture skips existing rows, then `current` is a false claim | Resolved | One memory row existed before `configure`. Status was `pending` before drain and `current` after. One note was written. |
| Obsidian 2. A legal memory body wedges the outbox, and a later small edit never publishes | Changed | `€` × 1,400,000 published, held 0, status `current`, one note. A task whose payload is over 16 MiB still wedges, and a later fitting revision never publishes. Finding 3. |
| Obsidian 3. Strict restore dies on `events.seq` when two task ledgers share a vault | Resolved | Two temp ledgers drained into one vault. `seq_remapped_tasks` 1, event seqs `[1, 2, 3, 4]`, restore errors 0. |
| Obsidian 4. `validate()` accepts a tombstone whose `-t` suffix was removed | Resolved | Renaming `*-t.md` to `*.md` makes `validate` return ok false with `tombstone-flag-disagrees`. Restore reports that ValueError and does not apply the tombstone JSON as an item (`restored` {}). Flipping `jarvis_tombstone` to false while keeping `-t` fails the same check. |
| Obsidian 5. Docs still say the engine is unwired | Resolved | `second_brain.py` docstring, `OBSIDIAN-STORAGE-IMPLEMENTATION.md`, and `OBSIDIAN-RUNTIME.md` say C2 write-through is wired and C3 is not. |

`tests/cluster/test_fleet_management.py::test_main_flipped_during_device_write` only covers the flip that happens before `_write_device` reads. That test passes and does not reach findings 1 or 2.

## Findings

### 1. High — the device write puts a stale config back, then the invite runs

`jarvis/fleet_management.py:498-510`. The re-read at `:720-731` runs after this write.

`_write_device` reads the whole config, copies a backup, then `atomic_write`s `{**fresh, "devices": devices}` from that earlier read. Anything stored in the file after the read is replaced. The re-read then sees the replaced file. If the lost edit was `main_device`, this PC still looks like the main and `cluster_setup.invite` is called.

Observed: during `shutil.copy2` (after the read, before the replace) the file was set to `main_device=dell` plus `audit_marker=concurrent-configure`. `issue_invite` returned a code. A follow-up that enrolled inside the mocked invite left `nodes` with one row for `oracle1`. The live file's main was `main` again, the marker was gone, and `devices.oracle1` was present. The backup still had `dell` and the marker.

`jarvis cluster main` takes `fleet-manage.lock` (`cluster_cli.py:80-86`) and cannot interleave. `jarvis cluster configure` writes the file with no lock (`cluster_cli.py:100-111`). That is the same-machine writer this window loses. This is not a cross-machine lease.

Minimal fix: re-read the file immediately before `atomic_write` and change only `devices.<node>` on that latest document. Add a regression that mutates the file inside the backup copy and asserts the invite is refused and the concurrent `main_device` is still on disk.

### 2. High — a role change during key generation still issues the credential

`jarvis/cluster_setup.py:437-479` and `jarvis/fleet_management.py:718-731`.

`issue_invite` re-reads and checks the main, then calls `invite` with that in-memory config. `invite` decides the role from that object (`identity(config)` at `:437`) and never reads the file again. `code_key` runs `ssh-keygen` before any credential (`:459`), which is the right order for a signing-key failure and is also a multi-second window. Because `_write_device` already inserted the node, the `save_json` at `:468-470` does not run, so the flipped file is left as it is while `authorize` and `Broker.enroll` still run.

Observed: `code_key` set `main_device` to `dell` and returned. `issue_invite` returned a code (`jarvis-invite-1:…`). The live file's main was `dell`. `nodes` had one row. `authorized_keys` gained a `jarvis-node:oracle1` line. The code is built from the pre-flip object (`:481`, `main_device`).

Same boundary as finding 1: `cluster configure` and `jarvis cluster invite` (`cluster_cli.py:209-211`) do not take `fleet-manage.lock`. The CLI invite also skips the receipt file. That overlap was not executed. The executed failure is the role flip inside `code_key` on the console path.

Minimal fix: re-read `path` inside `invite` immediately before `authorize` and refuse when this PC is no longer the main or the tunnel is no longer forward. Take the existing fleet lock in `cluster configure` and in the CLI invite. Regression: flip `main_device` inside `code_key`, assert no node row, no new key line, and no code.

### 3. High — an oversize held head blocks every later revision of that record

`jarvis/second_brain_queue.py:84-86` (`_HEAD_SQL`), `:228-233` (txn over the cap raises; baseline over the cap is held), and `jarvis/store.py:167-170`. Status: `jarvis/second_brain_runtime.py:440-441`.

A pending row is published only when its `seq` is the minimum seq of every outbox row for that key, including held rows. Drain releases a held oversize row only when `bytes <= cap` (`second_brain_queue.py:668-672`). A head that is still over the cap stays the head forever, so a later fitting revision stays pending. One such row keeps `archive status` at `held`.

Memory cannot build this. The body cap is 2,000,000 characters (`memory.py:225`), and the euro fixture published. History command text is truncated to 500 characters. Task `spec` and event `detail` are unbounded. `Store.transition` writes the event and then captures in the same transaction, so a payload that is still over 16 MiB rolls the state change back.

Observed, temp vault, capture armed after the insert:

- Baseline drain wrote no note. The outbox row was `held` / `oversize` / `origin=baseline` and larger than 16 MiB.
- `transition` to `failed` raised `ArchiveCaptureError`. The task stayed `queued`.
- The spec was then updated to `{"objective":"small"}`. Drain published 0.
- `transition` then succeeded (`failed`). Drain published 0 again. Note count stayed 0. Status stayed `held`.
- Outbox: seq 1 held oversize baseline, seq 2 pending reconcile (the shrink), seq 3 pending txn (the transition).

The rollback while the payload is still over the cap is the right refusal. The failure is that shrinking the source does not unblock the vault or the status word.

Minimal fix: when a newer revision of the same key fits under the cap, drop or skip older held-oversize rows for that key before choosing the head. Keep the `ArchiveCaptureError` rollback for a mutation that is itself over the cap. Regression: lower `MAX_RECORD_BYTES` (or insert one held oversize row and one later pending row), shrink, drain, assert a note exists and status can leave `held`.

### 4. Medium — restoring a failed invite puts the credential back and leaves its jobs uncertain

`jarvis/cluster_store.py:116-117` and `jarvis/cluster_setup.py:496-507`.

`Broker.enroll` rotates the token and sets that node's `running` jobs to `uncertain` in the same transaction. The comment says a rotated credential must not redeliver work. On a later exception, `_restore_node` puts back the prior token hash when this attempt still owns the row, and `_restore_key` removes only this attempt's key line. Neither function touches `jobs`. If both restores succeed, `invite` raises `InviteIncomplete(retryable=True)`.

The console refuses a node that is already online, offline, or waiting, so a normal first invite has no running jobs. The CLI `jarvis cluster invite` calls `invite` directly and will rotate a live node. A console `replace_pending` of an invite that already has running jobs hits the same compensator.

Observed: a live `dell` row and one `running` job. `encode` raised after `enroll`. Result: `InviteIncomplete` retryable true, token hash restored, authorized_keys unchanged, job state `uncertain`.

Minimal fix: snapshot non-terminal job states in `_node_snapshot`. If `_restore_node` puts the prior token back, put those jobs back too. Leave them `uncertain` when the new credential is kept. Regression: the fixture above, asserting the job is still `running`.

## What works

Confirmed in temp fixtures this session:

- Receipt file blocks a second code for an issued request id after the in-memory ledger is cleared, and the file holds no code.
- A signing-key `RuntimeError` happens before enroll, is retryable, and leaves no node row and no key line.
- A MAIN flip that is already on disk when `_write_device` reads is refused and does not enroll.
- Per-thread tool snapshots. A's tools survive B's later ask. `_merge_first_turn` does not write into a list the lane has replaced.
- Capture of a pre-existing memory row, then drain, reaches `current` with a note. A 1.4e6-character euro body publishes under the 16 MiB cap.
- Tombstone filename, frontmatter, and payload must agree. Both disagreement directions fail `validate` and are not applied as items.
- Two task ledgers whose events both start at seq 1 restore, with one ledger remapped.
- Queue `_build` and `SecondBrain._memory_records` produced the same canonical payload for one note.
- Phone fleet inventory: `can_write` false, no supported writes, no LAN address in the payload.
- Hostmeter units, all fake, no hardware. Windows `total_kb` 8388608 becomes 8589934592 bytes and the label `8 GB`. A 1 MB page file becomes 1048576 bytes. Disk sizes that are already bytes stay bytes. Linux `/proc` kB and probe `mem_total_kb` use the same 8 GiB figure. Probe `stat` blocks are `bsize * count` (4096 × 1000 = 4096000 bytes). 50,000,000 µWh renders as `50 Wh full · 50 Wh design`, health 100%. Energy 50% and charge 100% produce no health percent and the reason `energy 50% and charge 100%`. Win32 125% versus root\\wmi 83.3% likewise withholds the percent. A payload `health_percent` of 99 on a 100/100 supply is rebuilt as 100. `for_device` keeps a local sample (total 100) over a newer heartbeat (total 999). `accept()` stores byte totals and ignores a declared `KB` unit: a sender that puts a kilobyte count in `total` will be displayed as that many bytes. Current producers (`read_linux`, `windows_canonical`, `from_probe`) emit bytes.

## Keep, consolidate, remove

Keep the receipt file as the invite authority, `code_key` before enroll, owned-row and owned-key rollback, the per-thread tool snapshot, and one `build_payload` shared by the queue and the engine. The parity check matched. Do not add another serializer. Keep the 16 MiB cap and the transaction rollback when a mutation itself is over the cap. Keep phone `can_write: false`.

Consolidate config writes on the lock that already exists. `_write_device`, `cluster configure`, and the CLI invite should serialize on `fleet-manage.lock`, and the device write should patch the latest file. That is the fix for findings 1 and 2. It is not a new lock and it is not a lease.

Leave the in-memory invite ledger. The file already blocks a replay after restart. The ledger does not contradict the file in the repro. Removing it saves nothing important.

Do not merge the archive engine into the queue or the reverse. Their payload bytes already agree. The bug is head selection, not two builders.

## Not filed

- `source_gone` only increases. The repros never incremented it. While capture is on, history prune and task prune do not delete watched rows. A raw delete of a watched row would stick the archive in `pending`. Deletion is still not a tombstone. That remains the known C3 gap.
- Phone `/phone/api/fleet` still returns operational `status` such as `not_enrolled`. `phone.fleet`'s docstring says it omits invite state. Existing tests expect the status. The address strip on that payload held in the fixture. `phone.devices()` still copies `lan_ip` and a tailscale address onto the overview. That function was read and not executed here. It is a different route from the fleet projection.
- `accept()` ignoring a non-byte unit label, recorded above. No current producer sends kilobytes through it.
- Drain's `active.remove` during a store error is followed by `break`. It does not skip the next store. `last_drain_at` can be stamped while a held row remains. The verdict checks held before it will say `current`.
- Process-global hostmeter cache (20 s) and unmeasured Dell battery hardware. The fixtures above did not touch that machine.

## Fixes, in order

1. Patch `_write_device` onto the latest file (finding 1) and re-read the role inside `invite` before `authorize` (finding 2). Put `cluster configure` and the CLI invite on `fleet-manage.lock`.
2. Let a fitting revision supersede a held oversize head (finding 3). Keep refusing a mutation that is itself over the cap.
3. Restore running jobs when invite compensation restores the previous token (finding 4).
