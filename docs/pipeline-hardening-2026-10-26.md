# Plan pipeline ownership hardening

Date: October 26, 2026.

Parallel workspace writers now require at least one owned path. Plan validation rejects absolute paths, traversal, empty or ambiguous Windows names, and compares case, separators, dot segments, and nested directories consistently. Immediately before dispatch, the orchestrator resolves owned paths inside the plan workspace. This catches symlink aliases and symlinks that lead outside the workspace, including links added after plan creation. Invalid plans fail before tasks are added to the ledger.

The focused orchestration and recovery checks passed: 40 tests across `test_cognition.OrchestratorTests`, `test_pipelines.PlanHardeningTests`, `test_plan_recovery`, and `test_orchestration_lanes`. The full offline suite did not pass on this checkout; observed unrelated failures include speech threshold expectations, missing home-directory environment during the speech setup test, and a workflow routing expectation. No provider or hardware calls were needed for the focused checks.

Owned paths are planning and collision checks, not filesystem permissions for a provider process. A link can also change between preflight and an agent's edit. Processes still rely on their existing runtime permission mode and prompt scope. Plans saved before this change with workspace writers but no owned paths must be amended before they can run.

## Live feed recovery

A saturated subscriber queue now closes that SSE connection. The browser's existing EventSource retry receives a fresh `hello` event, which reloads authoritative history and overview state. The watcher also compares the opened feed file's identity, so a replaced `events.jsonl` is read from the beginning even when its size is equal to or larger than the former offset. The feed remains a bounded transient stream; changes written to a rotated file between polls may still require the browser's next authoritative reload.

Six focused bus, watcher, and SSE tests passed with an inert `jarvis.assistant` import stub. The normal `test_live` import remained blocked during this validation because an independent in-progress edit left `jarvis/assistant.py` with an indentation error. The regression tests are added in `tests/test_live.py` for the next combined run.
