# Independent pipeline audit

Date: 2026-10-21. Reviewer: the independently delegated `pipeline_auditor` subagent.

The reviewed changes address the reproduced faults without changing routing, model choices, provider permissions, or the single-running-task constraint. No blocking regression was found in the reviewed changes and offline checks. This is a scoped code and regression review, not a guarantee about live provider or hardware behavior.

## Scope and method

Reviewed the workflow from `assistant.ask` through the warm Claude/ChatGPT lanes, task runner, plan orchestration, console jobs, SQLite task ledger, and SSE event feed. Read the relevant existing tests and architecture documentation. Compared all six changed production files against the pre-change copies in `%TEMP%/jarvis-pipeline-baseline-20261021`:

- `jarvis/fastlane.py`
- `jarvis/chatgpt.py`
- `jarvis/orchestrator.py`
- `jarvis/store.py`
- `jarvis/ui.py`
- `jarvis/runner.py`

The reviewer did not edit production code or the implementer's regression tests. Tests used temporary state directories, mock providers, and locally spawned Python protocol fakes. No personal `.jarvis` contents, credentials, live model calls, paid services, microphone operations, or desktop actions were used.

## Findings verified as addressed

| Original fault | Reviewed change | Independent evidence |
| --- | --- | --- |
| A failed Claude turn could leave a result in its queue, and the next question received that stale answer. Non-`RuntimeError` callback failures could also contaminate ChatGPT's next turn. | Both lane entry points discard the process and queued state on abandoned turns; overload retry remains limited to the intended runtime error. | Reproduced stale Claude output before the change. Actual local fake-CLI callback failures now close both lanes, and the next turn answers afresh. See `fastlane.py:111` and `chatgpt.py:231`. |
| A continuously populated event queue bypassed the timeout because the old code only timed out an empty queue. Wall-clock changes could also distort the deadline. | Event loops explicitly check remaining monotonic time before consuming a queued event. | Before the change, both lanes accepted queued completion after the nominal deadline. The reviewed code rejects expired reads without consuming the queued event. See `fastlane.py:137` and `chatgpt.py:132`. |
| Lane shutdown did not reliably reap killed children or close stdout. The baseline recovery test emitted a `ResourceWarning`. | Shared shutdown waits after a forced kill; reader threads own and close stdout and signal EOF in `finally`. | Verified real local child termination, closed stdin/stdout, and stopped reader threads, including a child that ignored stdin EOF. The existing recovery suite no longer emitted the observed warning. See `fastlane.py:45`, `fastlane.py:92`, and `chatgpt.py:119`. |
| The task dashboard fetched every task twice and opened up to fourteen additional connections for recent task specifications. | One connection reads state totals and a limited recent-task projection in a transaction; a timestamp index supports recent ordering. | Compared the old and new response projections over temporary task records. Contract and truncated objectives are preserved. The regression test also confirms one connection and two SELECT statements. See `store.py:67` and `ui.py:121`. |
| The runner loaded the whole JSONL output and split it into another collection just to find its last answer. | Iterate the file line by line, retaining the latest matching answer. | The regression test prohibits `Path.read_text` while parsing the JSONL, and still obtains the last valid answer through malformed input and a final line without a newline. See `runner.py:75`. |
| A task could finish, then a memory-indexing failure could occur before its plan result was persisted. Resuming could execute completed work again. Direct snapshot writes could leave partial JSON. | Write plan snapshots through a temporary file, flush/fsync, and replace. Save task results before indexing memory. Refuse automatic replay when a dispatch is recorded without a result checkpoint. | Reproduced missing persisted results after a baseline memory failure. Now the result survives, and resume dispatches zero extra tasks. Serialization/replace failures preserve the prior plan and clean temporary files. An ambiguous dispatch is refused. See `orchestrator.py:131`, `orchestrator.py:185`, and `orchestrator.py:209`. |

## Commands and results

All commands were run from the project root with the local `python` interpreter.

```powershell
python -m unittest discover -s tests -p test_pipelines.py -v
# Baseline: 33 passed, 14.373 seconds; observed unclosed stdout ResourceWarning.

python -W error::ResourceWarning -m unittest discover -s tests -p test_pipelines.py -v
# Reviewed code: 33 passed, 14.116 seconds; no observed ResourceWarning.

python -W error::ResourceWarning -m unittest discover -s tests -p test_pipeline_optimizations.py -v
# Reviewed code: 13 passed, 1.211 seconds.
```

An additional reviewer-authored, in-memory unittest harness was run through a PowerShell here-string piped to `python -`; it did not add another repository test file. Eight checks passed in 5.718 seconds:

1. Claude callback failure closes the child/pipes/reader and the next turn succeeds.
2. ChatGPT callback failure closes the child/pipes/reader and the next turn succeeds.
3. A real local child that sleeps rather than reading stdin is killed and reaped.
4. An expired ChatGPT deadline rejects a queued event without consuming it.
5. A memory write failure after completed execution cannot cause reexecution.
6. A JSON serialization failure preserves the previous plan and removes temporary files.
7. A saved ambiguous dispatch cannot be executed automatically again.
8. The optimized task overview matches the original recent-task projection and excludes private evidence fields.

Elapsed times are test timings, not live-provider latency measurements. Warning-as-error mode alone does not prove all resource leaks absent; the explicit child/pipe/thread assertions supply the lifecycle evidence above.

## Remaining risks and recommended follow-up

These are existing limitations or deliberate boundaries of this change, not newly found blocking regressions.

| Priority | Remaining issue | Next action |
| --- | --- | --- |
| High, before concurrent execution | Plan files have no cross-process execution lease. Two invocations can load the same old snapshot before dispatch. The task ledger permits only one running task, but does not provide a whole-plan claim. | Add a durable plan lease and task/result reconciliation before allowing parallel orchestration or claiming exactly-once execution. Keep serial execution now. |
| Medium | Ambiguous dispatch recovery is deliberately conservative. A record can remain uncertain even when a provider preflight failed before launch. The new error asks for inspection; no automatic repair workflow exists. | Add a recovery command that inspects task state and artifacts, distinguishes never-started from possibly-executed work, and records an explicit recovery decision. Never blindly clear dispatch records. |
| Medium | A saved result whose later memory write fails remains absent from the memory index after resume. The execution checkpoint is preserved. | Add idempotent memory-index reconciliation keyed to plan and subtask, without rerunning execution. |
| Medium | Deadline checks bound the event-reading loops. Startup has separate timeouts, synchronous tool/speech callbacks can block, stdin writes are not timed, and graceful cleanup can add several seconds. | Measure these stages separately and provide bounded/cancellable tool and speech boundaries before promising an end-to-end deadline. |
| Medium | `close_process` owns the direct child. Forced shutdown does not guarantee descendant MCP/browser processes exit; a descendant retaining stdout can outlive the one-second reader join. | Track owned process trees or use platform process groups/job objects, with dedicated tests, before expanding long-lived tool workers. |
| Medium | Feed rollover detection only compares sizes (`events.py:166`). Replacement by an equal/larger file between polls can hide new events. | Track file identity as well as offset and size; cover equal-size replacement. |
| Medium | A full subscriber queue silently drops events (`events.py:42`), while the SSE connection can remain open. The comment's reconnect recovery is not guaranteed. | Add an explicit resync/overflow event or close the affected stream so the client reloads authoritative state. |
| Medium | Console conversation and plan jobs use separate admission checks (`ui.py:190`, `ui.py:487`), but a conversation that escalates or falls back can need the same single task-ledger slot. | Provide a clear contention response or scheduler. Preserve the existing single-running-task database constraint. |
| Low | Completed console jobs remain in memory indefinitely (`ui.py:72`). | Add bounded retention for terminal jobs while preserving active/recent polling clients and durable conversation history. |
| Before parallel writers | Owned-path validation only checks identical strings (`orchestrator.py:94`); parent/child directories and aliases can overlap. | Normalize scope and detect overlapping paths, then isolate workspaces before enabling parallel writers. |

## What to keep, add, and avoid

Keep the modular monolith, standard-library HTTP layer, SQLite/FTS5, lean/action conversation split, explicit plan execution, and offline protocol tests. These fit the current local-assistant workload and make the changes reviewable.

Add repeatable offline CI, recovery tooling, event-overflow tests, and stage latency measurements before adding infrastructure. Measure routing, recall, provider startup, first sentence, full answer, synthesis, and playback separately so future optimization targets the actual bottleneck.

Remove redundant reads and whole-log materialization, as this patch does. Do not remove functioning provider fallbacks or the serial writer constraint without replacement guarantees. Microservices, Redis, a vector database, additional model providers, and parallel writers are not justified by the evidence gathered in this review.
