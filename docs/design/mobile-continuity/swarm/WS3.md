State: done
Done:
- Archive queue/integrity (held-head, revision-gap, chunk-integrity, status holes) and scoped Obsidian canonical recall (`second_brain.canonical_read`, default false) accepted earlier; Luna re-audits clean. Full C3 cutover NOT done (see below).
- `jarvis/brainsync.py` (new), `jarvis brain sync|status` + hidden `export|receive|freeze` in `jarvis/__main__.py` (brain parser/dispatch only), `docs/design/mobile-continuity/BRAINSYNC.md`. Off by default (`brainsync.enabled` must be boolean true). `handoff(old_main, new_main, config, base, *, transport=None, between_reads=None) -> {ok, status, error}`; WS1 fences first and refuses activation unless ok.
- Wrap-up round (Sonnet 5.5 SWE after Codex quota ran out; no Luna audit, diff reviewed by the SWE; two Grok 4.7 coders):
  - HIGH finding FIXED: oversized/omitted note with handoff success. `_make_bundle` now raises `omitted_notes` when `_collect_vault` reports any blocking exclusion (too large, symlink, unreadable, bad path, secret); no bundle or pending file written.
  - Import fails closed: `_receive_unlocked` returns `materialize_incomplete` if any path was skipped (symlink collision etc.); memory not installed, source not frozen.
  - Replay/divergence: `freeze()` returns `source_changed` if source memory or vault differs from the pending bundle's recorded `memory_sha256`/`vault_sha256`; no marker written.
  - SSH adapter runs peer checkout venv with `--config`/`--state` when peer has POSIX absolute `jarvis_dir`; unsafe path or Windows peer -> `peer_path_unsafe`.
  - CLI: unknown `brain` action rejected before reading stdin; `export`/`freeze` refuse requests >4096 bytes (no truncation).
- Tests (focused, JARVIS_NO_MODELS=1): before round 57/64 pass (7 failures: oversized omission, symlink collision, source divergence, 2x 4096 cap, peer venv, unknown-action hang). After round: tests.data.test_brainsync, test_brainsync_adversarial, test_brainsync_cli, test_second_brain_canonical, test_ws3_archive_integrity, test_second_brain_overflow, test_second_brain, test_second_brain_queue, test_second_brain_storage, test_vault + tests.runtime.test_second_brain_runtime, test_archive_cli: 275 tests, OK (34s).
- Transport ACCEPTED LOCALLY ONLY: fail-closed in every adversarial case tested (omitted note, symlink, replay, source divergence, offline source, oversized request), proven with a local bare repo, two clones and an in-process stand-in for `deploy._remote`/ssh. NOT proven over real SSH, GitHub, Windows or the Dell. Do not enable in production until the items below are done.
Doing: nothing.
Next: (none started; all items below are in Cannot be done here)
Needs: WS1 call `brainsync.handoff` after fencing/quiescence; config base = checkout/config parent, state = base/.jarvis. WS5 integrate a brain-transfer proof in the multi-process drill (offline source cannot prove latest).
Decision: Which private GitHub brain repo/remote? Recommend dedicated private vault-only repo. `memory.sqlite3` to GitHub? Recommend no; direct cluster bundle only. Defaults `brainsync.enabled` and `second_brain.canonical_read` stay false. No new dependencies.
David runs: nothing performed live. Verified commands are in BRAINSYNC.md ("What David runs"). Before enabling: set `jarvis_dir` on every peer device in jarvis.config.json (David edits; backup first as `jarvis.config.json.pre-brainsync`).
Cannot be done here / Next:
- Live vault cutover, GitHub repo creation/push, real Windows/Dell handoff, service restarts: prohibited by charter.
- Peer with no `jarvis_dir` still runs bare login `python -m jarvis brain ...` (kept so existing in-process stand-in tests stay valid); fix = require `jarvis_dir` (change adapter + those tests) once peers have it set. Windows peers are refused (`peer_path_unsafe`); needs a Windows-quoting adapter and a real Dell run.
- Forward-only/pull-mode peers (no SSH server): adapter fails closed (`source_offline`); needs an existing-link delivery seam via hub (WS2/WS5 files).
- `memory_conflict`/`materialize_incomplete` leave state for manual review; no automated resolution UI.
- Full legacy SQLite-to-C3 cutover: needs read/write/retention policy decisions and cross-WS callers; scoped canonical recall must not be read as a full cutover.
- No Luna/Codex audit of the final round (quota); a Luna re-audit of `brainsync.py` is advisable when Codex is back. Freeze check `_source_unchanged` re-reads the whole vault (cost O(vault)); acceptable now, optimize later.
- Full test suite not run by WS3 (lead runs it on a quiet tree).
Files changed (WS3): jarvis/brainsync.py, jarvis/__main__.py (brain parser/dispatch), jarvis/second_brain.py, jarvis/second_brain_queue.py, jarvis/second_brain_runtime.py, jarvis/vault.py, tests/data/test_brainsync.py, test_brainsync_adversarial.py, test_brainsync_cli.py, test_second_brain_canonical.py, test_ws3_archive_integrity.py, test_second_brain_overflow.py, docs/design/mobile-continuity/BRAINSYNC.md, this file.
