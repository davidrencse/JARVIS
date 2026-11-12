# Cleanup proposal: old Markdown and tests

Survey only, 2026-11-11. Nothing was deleted or edited except this file. Evidence is grep, `wc -c`, git tracking state and an AST scan of `tests/`. Byte counts are exact where a size is given; "≈" marks a sum I did not re-measure.

**Gate:** every row touching `docs/design/mobile-continuity/` waits until the swarm is done. `swarm/CHARTER.md` still names several of those docs as inputs.

## Headline result

| | Count | Bytes |
|---|---|---|
| DELETE, untracked `.pre-*` backups (docs/tests only; git cannot restore them) | 8 files | 317,258 |
| DELETE, tracked docs, superseded | 8 files + 4 audit-evidence files | ≈ 223,800 |
| MERGE, then delete the source | 3 files | 26,212 |
| Tests removable with evidence | 0 methods firmly, 1 conditional | n/a |
| **Total** | **≈ 23 files** | **≈ 567 KB** |

The test suite is cleaner than the docs. An AST scan of all 188 modules (3,695 test methods) found:
- no two test bodies with identical AST (over 200 chars);
- no import of a `jarvis.*` module that no longer exists;
- only one same-name pair across files, and those two are not duplicates (see KEEP-notable).

So I propose no test deletions I can defend with evidence. The test table lists what is worth a decision.

## DELETE

| File | Bytes | Reason | Superseded by / nothing unique lost? | Links to fix first |
|---|---|---|---|---|
| `docs/design/mobile-continuity/DESIGN.md.pre-obsidian-cutover` | 116,254 | Backup, ignored by git (`.gitignore:13 *.pre-*`). Irreversible once deleted. | `DESIGN.md` is the edited copy. | none |
| `docs/design/mobile-continuity/design.json.pre-obsidian-cutover` | 88,354 | Same. | `design.json` | none |
| `docs/jarvis-os-architecture.md.pre-cleanup` | 44,232 | Same. | `docs/jarvis-os-architecture.md` | none |
| `docs/handbook/audit-2026-11.md.pre-cleanup` | 4,419 | Same. | `docs/handbook/audit-2026-11.md` | none |
| `tests/archive/cases.jsonl.pre-statussuffix` | 30,351 | Backup; not test-discovered. | `tests/eval/cases.jsonl` | `PARITY-QA.md:46,50` mentions `tests/archive`; reword |
| `tests/archive/test_clipsync.py.pre-dropzone-env` | 28,330 | Backup of a test that exists. | `tests/remote/test_clipsync.py` | same |
| `tests/archive/test_overlay.py.pre-import-fix` | 1,393 | Backup. | `tests/remote/test_overlay.py` | same |
| `tests/desktop/test_spotify.py.pre-native-search-check` | 3,925 | Backup. | `tests/desktop/test_spotify.py` | none |
| `docs/codex-briefs.md` | 14,217 | Labels itself "Historical task prompts"; assumes the Dell as main, reverse tunnel and MiniConda, all false today. No inbound link in any md/py/js/json. | `docs/jarvis-os-architecture.md`, `docs/architecture-guide.md` | none |
| `docs/handbook/phone-usability.md` | 3,417 | `PARITY-PHONE.md:251` says it describes a Locations tab the tree no longer has and "Current code and `test_location_ui_and_geolocation_removed` win". Not linked from `docs/handbook/index.md`. | `docs/handbook/phone.md` (protected) | repoint `PARITY-PHONE.md:251,284` |
| `docs/pipeline-independent-audit.md` | 10,315 | Oct-21/22 audit of the pipeline; `pipeline-architecture-review.md` says phase 2 supersedes its open items. | `pipeline-hardening-phase2.md`, `pipeline-hardening-2026-10-26.md`, `docs/reference.md` | `docs/reference.md:11` |
| `docs/pipeline-phase2-independent-audit.md` | 9,262 | Cited only by `pipeline-hardening-phase2.md`. | the phase2 doc | that one mention |
| `docs/audit-evidence/{feature-validation-baseline.txt, phase2-final-tests.txt, phase2-core-changes.patch, production-changes.patch}` | 155,632 | Raw Oct-27 logs and patches; the code they describe is in git history. Referenced only by the pipeline/feature-validation docs. I did not size `baseline-tests.txt` or `final-tests.txt`; they would follow. | git history | `docs/feature-validation.md`, `pipeline-hardening-phase2.md`, `provider-limits.md`, `pipeline-architecture-review.md` |
| `docs/design/mobile-continuity/AUDIT-SUMMARY.md` | 4,411 | Verdict of the 3,334-test run, now stale. | `SENIOR-SWE-RECONCILIATION.md`, `AUDIT-CONSOLIDATION-*` | sibling AUDIT-* docs |
| `docs/design/mobile-continuity/AUDIT-INTEGRATION.md` | 3,668 | Same stale log, status "ongoing". | `AUDIT-INTEGRATED-REPAIRS.md`, `SENIOR-SWE-RECONCILIATION.md` | sibling AUDIT-* docs |
| `docs/design/mobile-continuity/CONSOLE-LIVE-CHECKPOINT.md` | 13,301 | Paint-only skin notes with dead `/home/jdoe/.t3/worktrees/...` paths. | `PARITY-CONSOLE.md`, `PARITY-DESIGN.md` | `PARITY-DESIGN.md` |
| `docs/design/mobile-continuity/PHONE-LIVE-CHECKPOINT.md` | 9,624 | Same kind of note. | `PARITY-PHONE.md` | `PARITY-DESIGN.md` |

Not verified: I did not diff the pipeline audits line by line against the phase 2 docs. A reviewer should do that before deleting. The `jarvis/` code backups (`*.pre-cleanup`, `*.pre-revamp` and others, about 600 KB) are outside this task's scope and swarm-owned code; they are untracked too, so say if you want them in a later pass.

## MERGE

| Source | Bytes | Into | Unique facts that must move |
|---|---|---|---|
| `docs/handbook/mobile-targeting-changes.md` | 3,354 | `docs/handbook/phone.md` (protected; receives text only) | The "Send to" PC selector in mobile Ask; `jarvis/phone.py` validates the device and capabilities and submits with `origin_device=target`; the evidence limits (mocked hardware fixture, no physical phone to Dell run). |
| `LUNA-PROGRAM-REDUNDANCY.md` | 12,003 | `CONTINUITY-CONSOLIDATION.md` (cited by `tests/continuity/test_contract_consolidation.py:3`) | Its "Deliberate distinctions to preserve" list. Everything else is audit narrative. |
| `LUNA-CONTINUITY-REDUNDANCY.md` | 10,855 | `CONTINUITY-CONSOLIDATION.md` | Same list. It links `SONNET-INTEGRATION-HANDOFF.md` and `OPERATIONAL-INTEGRATION.md`; drop those links. |
| `PARITY-API.md` (keep file; move two facts) | n/a | `FLEET-API.md` (linked from `jarvis/fleet_management.py:12`, `jarvis/web/fleet.js:4`) | CLI `configure`/`main`/`invite` take `fleet-manage.lock`; a role flip during signing-key generation is refused. `PARITY-API` says these supersede stale notes in `FLEET-API`. |

## KEEP (notable, or needs a decision)

| File | Why |
|---|---|
| `FLEET-API.md`, `OPERATIONAL-CONTRACT.md`, `EXECUTOR-IMPLEMENTATION.md`, `GATEWAY-IMPLEMENTATION.md`, `MAIN-HANDOFF.md`, `OPERATIONAL-INTEGRATION.md`, `OBSIDIAN-STORAGE-IMPLEMENTATION.md`, `PARITY-DESIGN.md` | Code comments or docstrings link to them (`jarvis/continuity/*`, `jarvis/second_brain.py:19`, `jarvis/fleet_management.py`, `jarvis/web/checkpoint.css:7`). |
| `P1-BOUNDARIES.md`, `IMPLEMENTATION-BASELINE.md`, `CONTINUITY-CONSOLIDATION.md`, `AUDIT-OBSIDIAN.md`, `FINAL-ARCHIVE-REPAIRS.md` | Tests cite them (`test_harness_boundaries.py:3,32,35` build message strings from `P1-BOUNDARIES`). Editing the test strings is a prerequisite to any delete. |
| `SENIOR-SWE-RECONCILIATION.md`, `AUDIT-CONSOLIDATION-BACKEND.md`, `AUDIT-CONSOLIDATION-SURFACES.md`, `SONNET-INTEGRATION-HANDOFF.md` | `swarm/CHARTER.md` references them. Re-survey after the swarm. |
| `DESIGN.md`, `ARCHITECTURE.md`, `ACTION-PLAN.md`, `REVIEW.md`, `design.json`, `CONCURRENCY.html` | Core design set; cross-linked. |
| `docs/architecture.md`, `reference.md`, `provider-limits.md`, `hardware.md`, `migration.md`, `unified-desktop.md`, `agent-browser/README.md` | Linked from CLAUDE.md, README or code docstrings. |
| `docs/handbook/audit-2026-11.md`, `perf-2026-11.md` | Dated change logs; README, index and `jarvis-os-architecture.md` link them. Review for MERGE into `architecture-guide.md` later. |
| `docs/handbook/drag-drop-design.md` (27,150) | Unbuilt design spike; still the only record of its VERIFIED/ASSUMED evidence. |
| `.learnings/LEARNINGS.md` (23,831) | 20 entries; `.learnings` is in `jarvis/ui.py:68` and `tests/web/test_ui_cli.py:87` ignore lists, not a doc link. |
| `jarvis-memory/` | `memory/*.md` is a stale snapshot of the live `~/.claude/.../memory` (`david-machines`, `jarvis-project`, `jarvis-python-interpreter`, `jarvis-vault` differ; 5 live files are missing). `PARITY-CONSOLE.md:150` cites `jarvis-memory/memory/jarvis-vault.md`. Your call whether the repo copy should exist at all. |

## Tests

| Item | Evidence | Proposal |
|---|---|---|
| `tests/remote/test_lanmouse_fuzz.py:235` `test_manual_stop_sticks_until_boot` (`@unittest.expectedFailure`) | Its comment says "BUG (suspected)": a stop by hand before the first post-boot reading is undone by the guard. It is the only `expectedFailure` in the suite outside the chaos files' named LC/BR findings. | Fix the guard or drop the test. Input-sharing code, so not a delete without you. |
| `tests/remote/test_lanmouse_fuzz.py` `test_autostart_off_never_starts_after_boot` | Same "BUG (suspected)" comment but no `expectedFailure`, so either it passes (comment stale) or fails (should be marked). | Check, then fix the comment. |
| `tests/mobile/test_phone.py` around line 463, `RealShell.test_every_asset_the_shell_references_is_served` | `skipTest("the phone shell isn't built yet")` guards a branch that is now dead; `jarvis/web/phone/index.html` exists. | Remove the guard only; keep the test. |
| `tests/desktop/test_app_ui.py:384` and `tests/desktop/test_control.py:351`, both `test_terminal_check_uses_config_and_home` | Same name and subject, different code paths (`test_app_ui`: ref-based typing, node versus main config; `test_control`: `control.act`). Not duplicates. | KEEP both. |
| `tests/runtime/test_harness_baseline.py` | Characterizes legacy dispatch; its docstring says it is meant to change on purpose when the P2+ authority guard lands. | KEEP until the swarm lands, then re-review. |
| `tests/audits/*`, `tests/cluster/test_audit_cluster.py` | Named "audit" but each pins its own regression (brain failover, listen/autostart, cluster outbox). No overlapping names. | KEEP. |
| Environment skips (`test_voice_print.py:84` random clip, `test_main_only_voice.py` `RealConfigTests`, ssh-keygen/numpy/bwrap/node) | Skip on missing tool or machine config; no unconditional `@skip` exists. | KEEP. |

Not touched, per brief: `test_gates_redteam.py`, `test_ui_security.py`, input lifecycle/release tests, `tests/continuity/*`.

## Total

- Files: ≈ 23 (8 untracked backups, 16 tracked docs including 4 evidence files, minus overlap with MERGE).
- Bytes: ≈ 567 KB (317,258 backups + ≈ 223,800 tracked + 26,212 after MERGE).
- Tests removed: 0 firm, 1 conditional (`test_manual_stop_sticks_until_boot`).
