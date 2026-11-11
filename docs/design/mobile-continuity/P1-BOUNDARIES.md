---
status: assessment-complete-for-contracts
authorization: owner-requested-start-2026-11-11
updated: 2026-11-11
scope: docs + tests/runtime/test_harness_boundaries.py; no production code changed
---
# P1 boundary contracts — registry, executor dispatch, providers, state, phone/Console

Companion to IMPLEMENTATION-BASELINE.md (P0 inventory, §2 effect paths, §3 state),
P1-IMPLEMENTATION.md (topology facade) and P1-ROLE-WIRING.md (Remote target / confirmation
station). **P1 grants no authority.** Nothing below is a lease, ticket, epoch or admission
gate; "seam" means "an existing function whose contract and tests are enough to hang a P2
guard on". Line numbers are 2026-11-11 working-tree values (shared dirty checkout): re-grep.

Decision: **no new production code or wrapper.** The baseline's proposed permissive
"effect admission" function and a `submit(kind, payload, target, origin, approval)` facade
would be pass-throughs over `Remote.call` / `Hub.dispatch`, which already take an explicit
target and (via ContextVars + `Roles`) separate caller, approval station and target. Adding
them now would be an unused API shell. The real P1 deliverable is the inventory below plus
`tests/runtime/test_harness_boundaries.py`, which makes new sinks fail until classified.

## 1. Accepted seams (existing API + test proof)

| Boundary | Seam (accepted as-is) | Contract | Proof |
| --- | --- | --- | --- |
| Roles | `jarvis.fleet.legacy_topology.Roles`, `confirmation_station`; re-exported by `jarvis/topology.py` | local / configured coordinator / origin / approval station / target are distinct values; descriptive only | `tests/cluster/test_fleet_contracts.py` (39), `test_fleet_role_routing.py` (58) |
| Registry | `registry.INTENTS`, `get`, `risk`, `validate` | typed intent → executor class (`direct`/`grokbot`/`brain`/`vault`/`orchestrator`), per-arg validators, risk, observer | existing parser/registry tests + new inventory tests |
| Direct executor | `pipeline.DIRECT` → `Remote.call(device, action, value)` | every `direct` intent except `spotify_play` (deliberately Grok Bot) maps to a `remote.ACTIONS` entry with a valid arg name | `EffectSinkInventory` (new) |
| Remote dispatch | `Remote.call` (`remote.py`) target rule via `Roles.execution_target`; target==local → `run_here`; remote → `Hub.dispatch` / `/v1/control` `{node,kind,payload}` | explicit origin/target; patch limits preserved | role-routing tests; `test_harness_baseline.py` pins the missing envelope fields |
| Hub job kinds | `cluster.validate_action`, `IN_PROCESS_ONLY`, `execute_local` | closed kind set; `camera`/`locate` never start from a credential | `tests/cluster/test_cluster_validate.py`, `EffectSinkInventory` |
| Console submit | `Console.submit(text, origin_device=, approval_device=)` then `request_from` + `approvals_on` in `_work` (`ui.py`) | request origin and approval station are separate fields on the job | `tests/mobile/test_phone.py:798,826` (phone→job fields), `tests/cluster/test_fleet_role_routing.py` |
| Phone | `phone.py` ask: `request_from(None)` then `submit(origin_device=target, approval_device=main)`; main-only server (`is_node` refusal); per-phone ask `key` dedups a retried request | phone never picks MAIN; confirmations park on the PC; lost reply returns the same job | `tests/mobile/test_phone.py`, `test_phone_e2e.py` |
| Provider env | `fastlane.subscription_env()` used by runner/chatgpt/fastlane/knowledge | strips API keys and `CLAUDE*`; the single env chokepoint | `tests/runtime/test_runner.py:169-211`, `tests/security/test_claude_auth.py` |
| Node console API | `cluster.node_api` | GET allow-list, POST needs `ask` | **new pin**, see §4 |

## 2. Effect-path inventory: covered seam or owned exception

"Covered" = reaches the sink through an accepted seam above, so a P2 guard has one place to
stand. "Exception" = does not; the owner is who must close it, and in which phase. None is
closed by P1.

| P0 effect path (baseline §2) | Status | Owner / phase |
| --- | --- | --- |
| Strict parser → `pipeline.DIRECT` → `Remote.call` | covered (inventory test) | P2 admission at `Remote.call` |
| Brain/`DesktopTools`, MCP tools → `Remote.call` | covered by the same seam; the *spawned model process* is not (see §3) | P2 (identity in tool context), P6 (broker, supported classes) |
| Console `/api/ask`, voice daemon, phone ask → `assistant.ask` | covered up to `Console.submit` / `ask`; origin/approval fields explicit | P2 (stable ticket age), P4 (guard-aware voice/console) |
| `Remote.call` target==local → `run_here` | covered but **bypasses Hub** by design (no authority tuple) | P2 must put admission before `run_here`; P4 fence |
| `Hub.dispatch` MAIN target → `execute_local` | covered by `validate_action`; no authority tuple, no caller request ID | P2 (target-bound execution ID) |
| `Hub.dispatch` node target, node worker | covered; `issued` reset at hand-off, no `request_id` passed to `broker.enqueue` | P2 (original ticket age) |
| Standalone CLI `jarvis desktop/lanmouse/control/clipboard/sleep/send` | **exception**: constructs `Remote`/modules directly, no Hub | P2 mode guard outside `--state`; P4 |
| Offline SSH fallback in `Remote.call` | **exception**: runs managed CLI on node, bypassing agent/queue | P2 refuse-or-guard; P4 disable |
| `cluster main/configure/invite/revoke/release/update`, hub auto-update | **exception**: role/credential/code mutation, config-only authority | P2 (identity + mode store), P3/P4 CAS |
| Managed launchers (autostart units, tunnels, overlay `.vbs`, watchdog, Lan Mouse tasks, knowledge timer) | **exception**: per-PC locks only; hand-made units unknowable | P4 inventory + disable; unit text UNKNOWN |
| `transfer`, `storage`, `remote_view`, `camera`, `locate`, `grokbot` kinds | covered by Hub kind set; sink-local checks only | P2 per-kind admission |
| Runner `codex/claude` tasks, knowledge `claude -p` | **exception**: provider's own tools, outside JARVIS (design limit) | P6 classification; interim §3 |
| Model-lane spawns (ChatGPT app-server, FastLane, interpreter) | env chokepoint covered; tool mediation partial | P2/P6 |
| Console node API (`/api/*` for nodes) | covered by `node_api` allow-list; **content egress** | P2 (§4 gap 1) |

Classification of every `remote.ACTIONS` entry (read-only vs host effect) and the Hub kind set
live in the test file, not here, so they cannot drift silently.

## 3. Provider lanes (read-only child audit, spot-checked)

A read-only Claude child enumerated spawn sites; the points that matter were re-read in source.

| Site | Class | Refused on a node? | Hook |
| --- | --- | --- | --- |
| `chatgpt.py` `ChatGPTLane._spawn` (brain act lane, interpreter, inference) | act lane mediated by JARVIS tools (shell tools off, read-only sandbox); others no effect | brain via proxy lane; interpreter/preflight unchecked | `_spawn`, `subscription_env` |
| `fastlane.py` `FastLane._spawn` | **talk lane**: built-in `Read,Glob,Grep,WebFetch,WebSearch` (`LEAN_FLAGS`), not JARVIS-mediated | via node proxy | `LEAN_FLAGS`/`_args` |
| Claude brain (`inference.py`) | JARVIS tools mediated; built-in WebSearch/WebFetch are not | yes | `_args` |
| `runner.py` `run_task` | provider's own workspace-write tools; no JARVIS tools | yes (`runner.py` `is_node`) | `make_plan` |
| `knowledge.py` `_llm_answer` (`claude -p`) | provider's own tools; **omits** `--setting-sources project` and `--no-session-persistence` | CLI refused; console path unchecked | direct `subprocess.run`, no helper |
| `claude_auth.py` renewal | no effect; own env strip | unchecked | separate path |

Accepted seam: `subscription_env()` plus the four spawn helpers; **no wrapper is added** (it would
claim fencing P1 cannot provide). Honest classification today: ChatGPT act lane and Claude
brain tools = *mediated-by-JARVIS-tools*; FastLane, runner, knowledge = *unmediated-own-tools*;
interpreter/auth = *no-effect*. Not verified: Claude CLI `Read` behavior under `--permission-mode
plan`; whether codex persists sessions in `~/.codex`; whether read-only codex still has a file read
tool; interpreter/Grok preflight on a node.

## 4. Private state and real gaps (need code, not docs)

State owner today is documented only (baseline §3); **no function reports which device owns what**,
and none is added (no consumer). Egress audit of memory/vault/history/tasks/events:

1. **Content egress to nodes (P2, small).** `cluster.NODE_API_READ` / `_UNDER` include
   `/api/history`, `/api/history/commands`, `/api/events`, `/api/tasks/<id>`; a node with the
   opt-in `console` capability gets request/reply text and task evidence (`assistant.py` stores
   recalled memory in task evidence), contradicting the comment that memory is "left out on
   purpose". `/api/memory`, `/api/settings`, `/api/files` are denied. Pinned in
   `NodeConsoleReadsPinned`; fix = remove or redact those routes in `cluster.py` (outside this
   ownership; parent to schedule). Oracle never receives these today; this is node→PC exposure.
2. **Started-but-unfinished tool call replays on failover (P2).** `ChatGPTLane._tool` logs only
   finished calls (`ok is not None`); FastLane likewise. A usage-limit error between
   `item/started` and `item/completed` leaves `tool_log` empty, so `Failover.ask` re-runs the same
   turn on the Claude stand-in with the same effect tools; `assistant.py` can also start a runner
   task after a lane error with an empty log. Violates "ambiguous effects never replay". Pinned in
   `StartedToolCallReplaysOnFailover`; `Failover` already observes the started event via `on_tool`
   (`ok=None`) so the fix is local to `inference.py` / lanes.
3. **Talk lane and knowledge spawns bypass JARVIS gates (P2).** FastLane `Read/Glob/Grep` +
   `WebFetch` can read `.jarvis/*.sqlite3` or `~/.codex` and send content out; knowledge omits the
   runner's `--setting-sources project` guard, whose comment links that omission to API-key
   billing (hard rule: no pay-per-token). Not fenced by `terminal` sandbox. Unverified in the
   CLI; the flag gap is cheap and arguably should not wait for P2. Not pinned (would assert argv text).
4. `brain-budget.json` read-modify-write is not file-locked across processes (low; lost L3 count).
5. Provider sessions written outside `--state` (non-assistant claude runs, knowledge) — P2/P6.

Already adequate, no code: Hub `ask` jobs are not re-run (`request_id` dedup, running jobs become
uncertain on restart); node timeouts report "uncertain, do not retry"; brain tool steps are not
retried; env scrubbing under test; phone status is projected and redacted; `/v1/status` is
whitelisted metadata.

## 5. P1 acceptance status

| Criterion (baseline §5 / ACTION-PLAN P1) | Status |
| --- | --- |
| Topology facade, separated roles, parity | **Met** (P1-IMPLEMENTATION, 39 tests) |
| Remote target / confirmation station wiring | **Met** (P1-ROLE-WIRING, 58 tests); consumer side `confirmation_origin`, `here()`, `resolve()`, `run_here local==main` remain inline |
| Registry / executor dispatch contract | **Met by accepted seams** + inventory tests; no new API |
| Provider lane facade (report mediated vs legacy) | **Partial**: classified above, no facade code; strict claim impossible for legacy CLIs (design limit) |
| State facade (named owners) | **Partial**: ownership is documentation only; no accessor by decision (no consumer) |
| Phone/Console distinct origin/target/approval; disconnect never changes MAIN or replays input | **Met for contract** (explicit job fields, ask dedup key); phone dedup keys are in-memory so a MAIN restart forgets them; real-phone behavior **unmeasured** |
| Every §2 sink wrapped or listed as owned exception | **Met** (§2 table); none *closed* — closure is P2+ |
| No new port/daemon/state move/config key/credential read | **Met** (docs + one test file) |
| Full suite once at integration | **Parent**; this change ran only targeted modules (below) |
| Effect admission hook | **Deliberately not built** (see decision) |

This is a *boundary-contract* assessment, not an implementation-ready safety claim. No guarantee
about single-MAIN, fencing, replay or privacy follows from P1.

## 6. Measurements still unknown, and what they block

| Unknown | Blocks |
| --- | --- |
| Oracle VM CPU/RAM/disk/network, reachability from PCs and phone | P3 authority prototype sizing, P4 gateway pilot |
| Dell OS/arch, installed launchers/Startup contents, spare resources, real latency | P3 worker admission, P4 inventory/disable, P5 scheduling |
| Per-host `local_device` / `main_device` config values | P2 identity migration, P4 cutover |
| Provider login/quota state on Dell/Oracle; Claude/Codex CLI behaviors in §3 | P6 provider readiness and mediation claims |
| Hand-made unit text (`lan-mouse*.service`, knowledge timer, `grok-check.sh`) | P4 "old process cannot execute" claim |
| Real phone over cellular: reconnect, lost reply | P4 acceptance, P9 soak |

## 7. Recommended smallest P2 identity/mode slice

1. Stable per-installation identity file **outside** `--state` (one fixed per-user path) plus a
   durable mode record (`legacy` default) read by every entry point that reaches `Remote.call`,
   `Hub.dispatch` or `cluster_cli` role writes. In `legacy` mode behavior is unchanged; the slice
   only *records and refuses mismatches*: a second mode guard written by a different state dir
   fails closed. Standalone CLI and offline-SSH fallback (§2 exceptions) check it first.
2. One admission call at the two choke points already shown: `Remote.call` (before `run_here`)
   and `Hub.dispatch` (before `execute_local`/`broker.enqueue`), carrying `(origin, approval,
   target, operation_id)`. Only now is the baseline's "effect admission" worth writing, because
   it will have consumers and a refusal mode.
3. Pass the caller's request ID to `broker.enqueue` (the broker already dedups) and keep original
   issue time in the signed job fields; both are pinned today by baseline tests that must change
   on purpose.
4. Fix §4 gaps 1 and 2 (small, local, independently testable) before any phone/PWA work depends on
   them; fix the knowledge `--setting-sources` flag at the same time.

Run: `.venv/bin/python -m unittest tests.runtime.test_harness_boundaries` (10 tests, fakes only).
Mutation check: adding a fake entry to `remote.ACTIONS` fails 2 inventory tests.
