# Independent audit: plan recovery and copy collisions

Date: 2026-10-21. Reviewer: the delegated `pipeline_auditor` subagent, independent of production implementation.

No blocking regression remains in the reviewed phase 2 changes after the fixes described below. Cross-process plan ownership and explicit recovery now address the corresponding gaps recorded in [the first audit](pipeline-independent-audit.md). Copy changes address deterministic naming collisions; they do not make an SCP transfer atomic against concurrent destination writers.

## Scope and evidence boundaries

Reviewed `jarvis/locking.py`, `jarvis/orchestrator.py`, `jarvis/__main__.py`, `jarvis/storage.py`, and `jarvis/transfer.py`, plus the new recovery/collision tests and adjusted storage test fixture. The reviewer made no production-code or regression-test edits.

Verification used temporary state, synthetic task records, fake provider/SCP calls, the remote Python lookup script executed locally, and actual local Python child processes for lock ownership. No live SSH connection, paid provider, personal `.jarvis` data, credentials, microphone, or desktop interaction was used. Actual Codex/Claude subscription usage is outside this phase 2 audit and belongs to the separate provider-limit validation.

## Plan ownership and recovery

The implementation uses an OS-backed, nonblocking file lock for each plan (`locking.py:9`). Both `run` and `recover` acquire it before loading a plan snapshot. The lock file stays in place so separate callers continue to lock the same filesystem object. Ownership ends when its open handle closes, including on process death; no stale-PID timeout is needed for the plan lock.

Independent checks confirmed:

- A second handle in the same process cannot acquire the held lock.
- A second process cannot run or recover the held plan.
- Killing the lock owner releases ownership; the remaining lock file does not prevent later recovery.
- Failed operations and `KeyboardInterrupt` release ownership.
- Lock contention is detected before either operation reads a potentially stale plan snapshot.

Recovery is explicit (`orchestrator.py:245`). `jarvis orchestrate recover PLAN_ID` inspects; adding `--apply` reconciles durable state without calling a provider. Terminal task results are adopted from their original task IDs and artifacts. Failed, timed-out, and interrupted work remains blocked. Running or missing task records require inspection and are not automatically retried.

Queued tasks keep their original IDs. A recovery marker permits a later explicit `run`; the marker is consumed and checkpointed before dispatch. If a standalone task runner claims that same ID first, the ledger's queued-to-running compare-and-set refuses the duplicate claim. The reviewer reproduced this race and confirmed that no replacement task ID was created.

Memory indexing is separately checkpointed and can be retried without executing the task again (`orchestrator.py:236`). Repeated recovery preserved a single indexed result, including output longer than the plan's 20,000-character answer preview. A missing completed-task answer is reported through `missing_output`; the implementation does not invent an answer or rerun the task to obtain one.

## Copy collision changes

Storage copies normalize a local destination name before checking occupancy (`storage.py:202`). Each candidate is checked directly with `lstat`, rather than inferring absence from a directory listing limited to 3,000 entries. On Windows, the filesystem check observes the destination's actual case behavior. A broken symlink occupies its name without following the leaf to its target. Permission failures are not treated as a free name.

The reviewed transfer helper now:

- Parses filename separators without interpreting a Linux colon as a Windows drive prefix.
- Normalizes trailing dots/spaces and reserved Windows device basenames.
- Preserves distinct numeric collision suffixes within the destination filename budget.
- Includes directories when allocating a remote drop-folder name, while keeping the ordinary inbox response file-only.
- Uses `lstat` for local fetch occupancy, including broken symlinks.

Two additional deterministic gaps were identified during this independent review and fixed by the implementer:

1. **A directory could be mistaken for a free send destination.** `send` previously allocated from the file-only inbox list. SCP would copy into an existing same-named directory instead of the advertised destination, potentially colliding with a nested file. Allocation now requests directory occupancy as well (`transfer.py:115`).
2. **A long collision name could disappear from the next inbox lookup.** Generated names could exceed the inbox's 200-character recognition limit, causing a later send to reuse an already occupied name. Drop-folder collision names now stay within 200 bytes; general storage collision names retain a 255-byte budget. A repeated-send regression and an independent simulated send both confirm distinct, recognized names.

## Verification results

Executed independently from the project root:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_plan_recovery.py -v
# 13 passed; 1.630 seconds.

python -W error::ResourceWarning -m unittest discover -s tests -p test_copy_collisions.py -v
# 12 passed; 0.465 seconds, including the added long-name regression.

python -m unittest discover -s tests -p test_storage.py -v
# 13 passed; 1.552 seconds on the reviewed exact-lookup implementation.
```

The first recovery test run exposed a fixture expectation mismatch: `final_message` has always stripped surrounding answer whitespace, but the long-output fixture expected its trailing space. The fixture was corrected to use a normalized answer; production normalization was preserved. The final recovery run above passed.

Additional reviewer-authored checks were run through temporary, in-memory Python harnesses without adding duplicate repository test files:

- Seven recovery checks passed in 0.550 seconds: dry inspection does not mutate plan/memory, terminal adoption and memory repair are idempotent, failures remain blocked, running recovery is refused without mutation, locking precedes loading, a competing direct runner cannot cause a replacement dispatch, and missing completed output is explicit.
- A real child-process lock check verified same-process contention, cross-process contention, owner-death release, and retained lock-file identity.
- Filename checks verified sanitization collisions, Windows case collisions, broken symlink occupancy, and direct execution of the remote lookup script for existing/missing/outside-root candidates.
- A simulated repeated send of a 200-character name, with the original already occupied, produced two distinct names of at most 200 characters. Neither selected name was already occupied.

These are local correctness tests, not live provider, SSH throughput, or hardware acceptance measurements. The overall feature suite and provider-limit changes are reported separately.

## Remaining limits

1. **Concurrent copy publication remains unresolved.** Occupancy selection and SCP are separate operations for local and remote destinations. Another process can create or replace the selected path after the check. Strong no-overwrite guarantees require owned staging and an atomic no-replace publication operation, or an equivalent destination-side protocol. Do not advertise the current code as solving that race.
2. **Recursive source symlinks remain unresolved.** Root checks resolve the selected top-level source, but recursive SCP may follow symlinks inside that directory. A child symlink can therefore refer outside the configured roots. Add a deliberate recursive symlink policy before claiming that all recursively copied content stays within the roots.
3. **Remote collision checks can add latency.** Each occupied storage candidate currently incurs another SSH lookup. A single remote candidate-selection loop would reduce round trips when many numbered copies already exist; it would still need separate atomic publication to solve concurrent writers.
4. **Locks are for a local filesystem.** This implementation does not establish distributed ownership across network filesystems or several machines sharing state. Keep the current local-state deployment assumption.
5. **Recovery preserves execution status, not acceptance verification.** A task with a successful recorded exit but missing artifacts can be restored as finished and flagged `missing_output`; dependent work may continue with no recovered answer. Inspect that flag and artifacts when deciding whether the intended work was actually achieved. Recovery does not automatically retry failed or running tasks.
6. **A parent crash does not prove all provider descendants stopped.** The plan lock is released by the OS when the parent exits, but process-tree cleanup remains a separate lifecycle concern. Running task recovery is intentionally refused rather than assuming that descendants are gone.

The single-running-task constraint remains intact. Parallel writers, automatic retries of ambiguous work, and destructive cleanup were not introduced by these changes.
