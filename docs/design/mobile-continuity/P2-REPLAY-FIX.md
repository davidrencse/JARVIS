---
status: implemented-targeted-tests-only
updated: 2026-11-11
closes: P1-BOUNDARIES.md §4 gap 2 (started-but-unfinished tool call replays on failover)
scope: jarvis/inference.py, jarvis/assistant.py (error/fallback only), tests
---
# P2 prep: a started tool call is never replayed

**Not a lease, ticket or fence.** This is a conservative no-replay rule inside one process. It
does not stop a second MAIN, a second JARVIS process or a node from running the same effect.

## Finding (was real)
`ChatGPTLane._tool` and `FastLane` add a call to `tool_log` only when it *finishes*
(`ok is not None`) but report every call to `on_tool`, with `ok=None` at the start. A usage
limit (or crash) between start and finish left `tool_log` empty, so:
1. `Failover.ask` saw "nothing ran" and re-asked the Claude stand-in, which has the same effect tools;
2. `assistant.ask`'s own fallbacks (`_standin_reply`, ledger `run_task`, one-shot `_direct_reply`)
   and `_orchestrated`'s "return None, let the brain lane answer" did the same.

## Fix
- `Failover` gives each lane its own relay as `lane.on_tool` (`_relay_for`, installed on the primary
  at construction and on every stand-in at creation). The relay first records the tool name on
  the in-flight request record(s), then forwards to the caller's callback (`Failover.on_tool`,
  now only stored, never put on a lane). Callback absent: still recorded. Callback raises:
  swallowed like the lanes already did, after recording.
- One `_Request` per `Failover.ask`, on a per-thread stack (`self._local.stack`): concurrent
  requests have separate records; a nested `ask` on the same thread gets its own record, and a
  tool started inside it also marks the outer one (the nested ask is part of the outer turn).
  A callback arriving on a thread with no request (not what the lanes do today: both emit on
  the asking thread, the reader thread only queues lines) is charged to every request running
  that lane: over-blocking is safe, missing a start is not.
- Primary failure: failover to the stand-in only if it is a limit **and** nothing was said **and**
  `tool_log` is empty **and** no tool started. Any started or finished tool blocks it.
- Every exception leaving `Failover.ask` (limit, other error, stand-in failure) gets
  `exc.tool_unfinished = [names]` for call keys still pending in that request (merged with lane evidence). Completed callbacks remove their matching key; repeated tools are distinguished by call key. The original
  error text is not rewritten, and `exc.spoken` is set as before for limits.
- `assistant.py` reads it in the lane-error handler: `ran or started` produces `_partial_reply`
  ("X started but I never saw it finish, so I can't tell whether it happened"), `detail["tools"]`
  gets `[name, None]` (unknown, not `False`), and no stand-in/ledger/one-shot run follows.
  `_orchestrated` no longer hands a failed orchestration back to the brain lane when a
  tools step had run or started. Completed calls keep their old wording ("ran"/"failed").
- A pure no-tool limit still fails over; a limit seen before the ask still goes straight to the stand-in.

## Tests
`tests/intelligence/test_inference_started_tools.py` (21): start→limit without finish; callback
absent; callback raises; completed call; pure no-tool fallback; limited up front; start→non-limit
error; start on the stand-in; callback from another thread; no leaked state after an ask or
`close()`; concurrent requests; nested requests (both ways); assistant (limit, non-limit, completed,
pure fallback, orchestrator, wording). `tests/runtime/test_harness_boundaries.py`:
`StartedToolCallReplaysOnFailover` became `StartedToolCallBlocksFailover` (assertion inverted), and
`NodeConsoleReadsPinned` became `NodeConsoleReadsDenied` (the four content routes now denied; the
other agent removed them from `cluster.py`). One existing test
(`test_inference.test_tool_reports_follow_the_lane_that_answered`) asserted the old mechanism
(lane holds the raw callback); it now asserts the behavior (callback still sees the stand-in's events).
Mutation check: removing `or request.tools` from the failover guard fails 10 tests.

## Parent follow-up and remaining limits

Parent closed the inner ChatGPTLane serverOverloaded retry gap: per-turn pending call keys
are recorded before callbacks, attached to original/wrapped exceptions, and prevent internal
fallback-model replay. Completed calls retain the existing tool log protection; a safe
pre-tool overload still retries. Pending state resets on the next turn.

Parent also records successful orchestration tool steps so a later planner failure cannot
silently hand the whole request back to the brain. A completed call of a tool name no longer
hides a second unfinished call with the same name.

Focused integration: 59 tests OK across test_chatgpt_started_tools, inference_started_tools,
inference and inference_races. Seven inner-lane/call-identity regressions and one successful-step
orchestration regression were added. Combined full suite is still pending; an initial run was
intentionally stopped before the final orchestration correction, so it has no pass result.

Legacy provider subprocess tools remain unmediated; this is conservative observed-event
no-replay, not an authority fence or proof an effect did/did not happen. Read-only tool starts
also prevent replay. No deployment or service restart occurred.
