# Pipeline hardening and validation: phase 2

Date: October 21, 2026. This report follows the [architecture review](pipeline-architecture-review.md). It records implemented changes separately from remaining recommendations.

## Implemented

### Plan execution and recovery

`jarvis/locking.py` provides a nonblocking OS file lock, held before loading a plan and throughout execution or recovery. A competing process fails with a busy message. Process exit releases the lock; the lock file intentionally remains. This coordinates one local filesystem, not separate state directories or distributed workers.

`jarvis/orchestrator.py` now supports explicit recovery without provider execution:

```powershell
python -m jarvis orchestrate recover PLAN_ID
python -m jarvis orchestrate recover PLAN_ID --apply
python -m jarvis orchestrate run PLAN_ID
```

The first command reports recorded task states. Applying recovery adopts terminal task artifacts, preserves failed work as blocked, and marks original queued task IDs for a later explicit run. It never creates replacement tasks for an uncertain dispatch. The ledger's atomic task claim still protects against a standalone runner racing that later run. Running or missing tasks stop recovery for inspection.

Result checkpoints precede memory indexing. Indexing can be repaired using the original artifact and content deduplication without executing completed work again. Missing terminal output is reported explicitly. A `finished` task still means process success, not independently verified acceptance criteria.

### File-copy naming

`jarvis/storage.py` checks exact candidate paths instead of relying on truncated directory listings. Both local and remote checks count directories and broken symlinks as occupied; permission errors fail closed. Parent paths remain subject to the allowed-root check.

`jarvis/transfer.py` normalizes names before collision detection, handles Windows reserved/trailing-dot names and colon-containing remote names, and preserves collision suffixes within filename byte budgets. Remote drop listing includes directories when choosing a destination, preventing accidental nesting into an existing directory. Drop names remain within the inbox parser's 200-character limit across repeated copies.

These changes fix deterministic collisions. Choosing a name and running SCP remain separate operations: simultaneous independent writers can still race. Atomic reservation/staging is follow-up work, not a guarantee of this patch.

### Provider quota reporting

Claude and Codex now report account-specific usage percentages and resets from authenticated first-party usage endpoints, with bounded requests, redirect refusal, sanitized errors, cached success/failure, and local fallbacks. Expired Codex log windows no longer masquerade as fully available quota. See [provider-limits.md](provider-limits.md) for timestamped account readings, official references, tests, and the internal-endpoint compatibility limitation.

### Microphone lifecycle

The feature-validation agent found that failed AudioWorklet startup could leave microphone tracks open. Each startup now owns its resources, releases them on failure/cancellation, and cannot overwrite a newer recording. Concurrent starts share one request. The console can cancel pending startup through Escape, the microphone button, or a held Space release; a quick Space tap retains its previous behavior. Animation callbacks are bound to their recording session, and a failing UI callback cannot bypass resource cleanup.

## Independent review and verification

Three agents worked on feature validation, pipeline audit, and provider limits. The independent phase 2 audit (retired; see git history) passed 13 recovery, 12 collision, and 13 existing storage tests. It independently exercised OS-process exclusion and crash release and identified two naming cases corrected before final validation.

The [feature matrix](feature-validation.md) distinguishes actual local process/HTTP/SQLite checks from mocked providers, browser APIs, and untested physical devices. Provider implementation added 12 focused tests; microphone work added 10 Node-backed regression scenarios including console cancellation wiring. The final combined run passed **243 tests in 143.212 seconds, with no failures or skips** (`python -m unittest discover -s tests -v`). The raw log was retired; see git history. This is 47 additional tests beyond the phase 1 total of 196.

All 30 Python modules parsed and all eight browser JavaScript files passed `node --check`; see [syntax evidence](audit-evidence/phase2-syntax.json). A final account-only check found Codex quotas live and Claude's sign-in expired; the [provider report](provider-limits.md) clearly separates that final result from the earlier successful Claude snapshot.

Follow-up at 10:32 PM EDT: opening Claude Code's built-in `/usage` screen renewed its existing session. The JARVIS collector then returned live Claude quotas (94% five-hour and 86% weekly remaining). See the updated provider report and snapshot. No production-code change or inference was needed for this renewal.

Follow-up on October 22: quota checks now renew an expired Claude session automatically through an initialize-only CLI startup, verify the renewed token, and report revoked sign-ins clearly. Ten integration tests were added, and a signature-tracking bug that hid the "run claude auth login" message was fixed. The full suite passed 293 of 293. See [automatic renewal](provider-limits.md#automatic-claude-session-renewal-october-22-2026).

Two agents reached the Codex account usage limit after saving their changes and findings. The primary agent completed the remaining code review, documentation, and combined regression run. Do not interpret this as a completed independent review of every final line: the pipeline audit is independent; final integration review is by the primary agent.

No paid model inference, real remote file copy, physical microphone recording, desktop launch, deployment, or purchase was used for the regression checks. Quota verification made read-only account requests. Agent activity itself consumes the coding account's allowance.

## What to add next

1. Atomic destination reservation or transfer staging with explicit concurrent-writer semantics; define recursive source-symlink handling before claiming strict root isolation for SCP copies.
2. Browser end-to-end tests covering conversation, files, credits, and microphone permission failures. Current JavaScript tests use mocked APIs, not a rendering engine.
3. Real-device acceptance checks for audio, overlay focus, Lan Mouse handoff, and interrupted cross-machine transfers.
4. A deliberate distribution contract. The documented source checkout works; the isolated wheel lacks default configuration and specialist resources. Keep personal configuration out of packaged defaults. The optional voice dependency list also needs reconciliation with the documented manual Kokoro installation.
5. Process-tree cleanup for provider descendants, bounded background scheduling, explicit cancellation, and semantic acceptance checks before any automatic retry policy.
6. Remote collision selection in one SSH operation instead of one connection per occupied candidate, once correctness is preserved.

## What to remove or avoid

- Remove stale claims that Claude quotas cannot be shown, that expired log windows prove quota refills, or that copies are atomic merely because they choose unused names. README and provider reporting were corrected accordingly.
- Avoid duplicate task creation during crash recovery and replay of completed provider work solely to repair memory indexing; the new recovery path handles both.
- Keep the current small local architecture. A distributed queue, service split, or broad framework rewrite is not justified by the observed workload and would increase the failure surface.
- Do not remove working integrations solely because their hardware cannot be exercised offline. Track their acceptance evidence explicitly.

Production changes remain in the working directory; this folder has no Git repository. Original root-owned phase 2 files were backed up under the session's temporary `jarvis-pipeline-phase2-20261021-120005` directory. The phase 1 report and evidence remain historical records rather than being overwritten.
