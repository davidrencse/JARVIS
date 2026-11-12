# Archive gap and Lan Mouse pair repairs

Date: 2026-11-11. Closes findings 4 and 5, and the stale-memo half of finding 6, in
`AUDIT-GROK-FINAL-VERIFICATION.md`. Findings 1–3 (revoke, enroll admission, receipt
replace) were not touched. No commit, push, CI, live config, vault, broker, daemon,
or service restart. Tests used temp directories and mocks only.

## Finding 4 — a gap hid a damaged head

`published_readable` and `publication_sound` returned true on `conflict` without
opening the newest note. Status then counted that head as checked and healthy, so
`state` became `current`. Drain dropped the matching backlog row. Rescan never
appended a repair, and it also did not put the deleted revision back.

What happens now:

- A tombstone is still acknowledged without `_load`, so a damaged chunk cannot block
  a deletion.
- Any other newest note is opened through `_load`. A gap is not a pass.
- `archive_chunk_damaged` counts a newest note that does not read back.
  `archive_revision_conflict` counts a record whose note names are not `1..n`,
  including a head status does not open. One damaged head makes `state` `error`.
  A gap with a readable newest note makes `state` `conflict`. Neither word is
  `current`.
- Rescan still only seeds. Drain appends the next revision only when the newest
  filename claims the live payload and `_load` says it does not hold it. The missing
  numbers are not written. An intact head with a gap stays acknowledged and is not
  copied forward. A different payload across a gap is still
  `revision-gap-or-duplicate` for the owner. `latest()` still raises on a gap.

## Findings 5 and 6 — pair, start, and the clipboard

`sharing_availability` is the only reading.

| Decision | Fresh `state` apart | Stale or missing timestamp, `settled` apart | `off_when_apart` false |
| --- | --- | --- | --- |
| `start_blocked` / `control("start")` | refuses | allows (a dead guard must not trap a manual start; a hand-start override still refuses) | allows |
| `pair_blocked`, `sharedinput.pair`, `jarvis lanmouse pair` | refuses before `pair()` and before either config write, stop, or start | same, with the "nothing was written" sentence | does not refuse |
| `sharing_paused` (clipboard) | "the laptops are apart" | same; the switch is not consulted | still pauses on settled apart |
| `write_pairing` called directly | writes the local file, does not restart | writes the local file, does not restart | may restart a running daemon |

`jarvis/__main__.py` checks `pair_blocked` before `sharedinput.pair`. `pair()` checks
again before `input_info`, the peer `lan_mouse` write, and `write_pairing`. Console
`peripherals.act` already refuses a fresh apart via `start_blocked`. A stale apart
reaches `pair()`, which then refuses before any write.

## Files

- `jarvis/second_brain.py` — head check, conflict count, append-only repair
- `jarvis/second_brain_queue.py` — rescan docstring only
- `jarvis/second_brain_runtime.py` — `archive_revision_conflict`, verdict `conflict`
- `jarvis/sharedinput.py` — `pair_blocked`, clipboard via the projection, no stale restart
- `jarvis/__main__.py` — `lanmouse pair` only
- `tests/data/test_second_brain_overflow.py`
- `tests/remote/test_lanmouse_guard.py`

Not edited: `cluster_cli.py`, fleet receipt code, `jarvis/continuity/`, live config, the vault.

## Tests

Temp fixtures. `JARVIS_NO_MODELS=1`. `.venv/bin/python`. No daemon, no live config write.

| Test | What it pins |
| --- | --- |
| `Recovery.test_gap_and_corrupt_head_is_not_current_and_rescan_does_not_fill_the_gap` | Two chunked revisions, delete rev 1, flip one rev-2 chunk byte. Status is `error`, damaged ≥ 1, conflict ≥ 1. Rescan plus drain appends rev 3 only. Rev 1 is not recreated. Old note and old chunk bytes stay. The new head reads back (`archive_chunk_damaged` 0) and status is `conflict`, not `current`. `latest()` still raises. `validate` reports the gap and `chunked-payload`. A second rescan does not append rev 4. |
| `ApartPairPolicyTests.test_pair_refuses_fresh_and_stale_apart_before_any_write_or_restart` | Fresh, age over 300 s, and a missing timestamp. `pair()` raises before `remote.call`, `write_pairing`, `local_info`, `lan_mouse_status`, `start`, and `stop`. The temp config bytes are unchanged. |
| `ApartPairPolicyTests.test_cli_pair_refuses_fresh_and_stale_apart_before_pair_runs` | `jarvis lanmouse pair` returns 1 with the apart sentence. `pair` is not called. Temp config, guard memo, and temp Lan Mouse file are unchanged. |
| `ApartPairPolicyTests.test_start_allows_stale_apart_and_clipboard_still_pauses` | Stale apart: `start_blocked` is None and `control("start")` calls the patched `start` once. `sharing_paused` is still "the laptops are apart". |
| `ApartPairPolicyTests.test_switch_off_does_not_take_the_apart_refusal` | `pair_blocked` is None. The clipboard still pauses. `pair()` reaches `input_info` and stops on a missing certificate, before `write_pairing`. |
| `ApartPairPolicyTests.test_direct_write_pairing_still_writes_but_does_not_restart_when_stale` | The low-level writer still writes. Patched `start` and `stop` are not called. `restarted` is false. |

Commands that passed:

- `tests.data.test_second_brain` `tests.data.test_second_brain_overflow` `tests.data.test_second_brain_queue` `tests.data.test_second_brain_storage` `tests.runtime.test_second_brain_runtime` `tests.runtime.test_archive_cli` `tests.runtime.test_archive_history_faults` — 193 tests, OK (26 s). The overflow module includes the new gap test and the existing contiguous-damage tests.
- `tests.data.test_second_brain_overflow.Recovery` `tests.data.test_second_brain.Integrity` `tests.runtime.test_second_brain_runtime.Status` `tests.remote.test_lanmouse_guard` `tests.remote.test_peripherals` `tests.desktop.test_controller_pause` — 112 tests, OK.
- `tests.remote.test_transfer_cluster.LanMouseTests` was in the first focused run and passed (the fresh `write_pairing` still writes and does not restart).

The parent `unittest discover -s tests` was not run.

## Remaining limits

- A tombstone is not opened by status or by `publication_sound`. A gap whose newest note is a tombstone is `conflict` when the names are not `1..n`, and drain does not resurrect it.
- The deleted revision and its chunk files stay missing or left behind. There is no garbage collection. `validate` still lists the gap and the damaged old chunk after the repair. `current` is not claimed.
- `latest()` and strict `restore` still stop on a gap. The appended revision does not pretend the history is whole.
- A source change while the newest note is intact and the names have a gap is still held as `revision-gap-or-duplicate`. It is not published across the gap.
- Duplicate revision numbers are the same conflict. They are not deleted or renumbered.
- A plain note larger than 64 KiB with no `.jrc` beside it is still not opened. A gap in its names still counts, so that record alone is not `current`.
- `write_pairing` and the desktop JSON `pair` action still write the local Lan Mouse file while apart. They do not restart the daemon for a fresh or stale apart. They do not write the peer's file. Only `sharedinput.pair` and `jarvis lanmouse pair` refuse before both writes.
- `start` on a stale apart memo is allowed, unless the memo also says the daemon was started by hand. That is deliberate.
- The clipboard pauses on settled apart and ignores `off_when_apart`. Turning the switch off lets start and pair proceed; it does not resume the clipboard.
- `peripherals.act` was not changed. It still refuses a fresh apart before calling `pair`. A stale apart is refused inside `pair`.
- `set_overflow` can still record `ok` false after an earlier ledger was updated. Not this change.
- No MAIN lease, no gateway handoff, no C3 canonical read. `jarvis/continuity/` was not activated and was not edited.
