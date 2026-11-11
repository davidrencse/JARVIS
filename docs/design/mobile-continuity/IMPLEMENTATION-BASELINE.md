---
title: P0 implementation baseline and P1 facade gates
status: repository-inventory-complete
authorization: owner-requested-start-2026-11-11
updated: 2026-11-11
scope: documentation + one characterization test; no runtime change
---

# P0 inventory: what the code does today

Companion to ACTION-PLAN.md (P0/P1), MAIN-HANDOFF.md §1/§9 and DESIGN.md §3/§17. This file
records **existing legacy behavior** only. Everything under "P1 contracts" is a **future
requirement**, not code. Line numbers are working-tree locations (checkout is dirty and another
lead is editing `jarvis/fleet/` and `jarvis/topology.py` right now), so re-grep before relying on them.
Host, service, provider and Oracle facts are **UNKNOWN** unless a repo source or file metadata
proves them; no live service, credential or state body was read to write this.

Evidence: static read of repo sources, `ls` of `.jarvis/` (names/sizes only), one read-only
Explore child (launcher/entry-point audit), and targeted test runs (end of file).

## 1. Role and authority today

Role is **config-derived**, not durable authority:

| Fact | Evidence |
| --- | --- |
| Local ID from `cluster.local_device` or unique hostname match; MAIN = `cluster.main_device`; `is_node = enabled and local != main` | `jarvis/fleet/legacy_topology.py:37-50` (`enabled`, `identity`), `jarvis/topology.py:17` (`is_node`, composed from the module-level names so test patches still apply) |
| `local_device: "auto"` depends on `socket.gethostname()` | same, `identity`. Which value applies on each real PC: **UNKNOWN** (config values not read) |
| Only per-PC, per-state-dir file locks stop two Hubs on **one** PC: `cluster-main.lock`, `cluster-node.lock` | `jarvis/cluster.py:898`, `:1433` (child audit) |
| Nothing arbitrates **between** PCs: each trusts its own copy of `jarvis.config.json` | pinned by `tests/runtime/test_harness_baseline.py::test_config_file_alone_decides_main_so_a_stale_copy_still_claims_it` |

### Legacy role/authority mutation paths

| Path | Evidence | What it does |
| --- | --- | --- |
| `jarvis cluster main <id>` | `jarvis/cluster_cli.py:77` → `set_main` `:52-65` | Rewrites `cluster.main_device` with `save_json`; no lock, confirmation, fence, token rotation or check that the old MAIN stopped. Returns text telling David to copy the config and restart services by hand |
| `cluster configure / init / join / leave` | `jarvis/cluster_cli.py:79-142`, `:183` | config + `cluster-node.json` credential rewrite, starts agent/autostart |
| `cluster invite / enroll / revoke / caps` | `jarvis/cluster_cli.py:188-224`, `jarvis/cluster_store.py` | issues/revokes node tokens and capabilities (`cluster.sqlite3`) |
| `cluster release / update [--rollback]` | `jarvis/cluster_cli.py:146-166`, `jarvis/deploy.py:461` | signed code push + remote restart |
| Hub auto-update and keep-up loops | `jarvis/cluster.py:500-547`, started `:937-939` | code push and node restarts without a human step |
| Settings UI / `voice use` writing config | `jarvis/settings.py` (FIELDS), `jarvis/__main__.py:902-905` | other writers of `jarvis.config.json`; role-key coverage **not verified** |
| Owner token | `jarvis/cluster_store.py:35,70-71` (`cluster-owner.token`), `jarvis/cluster.py:488-496` | any same-user process sharing the state dir can read it and act as owner; `X-JARVIS-Main` header is compared to config only |

## 2. Effect-path inventory

"Gate today" lists what stands between the caller and the effect. **No path consults a durable
epoch, operation ID or MAIN tuple.**

### 2.1 Interfaces → effect

| Interface | Entry (file:line) | Reaches | Gate today |
| --- | --- | --- | --- |
| CLI `jarvis desktop <action>` | `jarvis/__main__.py:404-410` → `Remote.call` | any `remote.ACTIONS` effect on any device | Node block-list `__main__.py:270-273` covers other commands; `desktop` itself runs directly |
| CLI `ask/chat/run/orchestrate` | `jarvis/__main__.py:756-870` | `assistant.ask` → brain/runner | `runner.run_task` refuses on node (`jarvis/runner.py:126-129`) |
| CLI misc effects | `lanmouse` `__main__.py:347-403`; `control stop` `:225-240`; `sleep/wake` `:211-224`; `clipboard` `:331-346`; `send/fetch` `:465-472` | input capture, power/units, clipboard, transfer | none beyond each module's checks |
| Voice daemon | `jarvis/daemon.py:573` (`ask(..., channel="voice")`); node path proxies to main (`daemon.py:112-113`, `is_node`) | full ask pipeline | `voice.lock` (`daemon.py:39-60`); main-only voice test `tests/voice/test_main_only_voice.py` |
| Console (desktop browser) | `jarvis/ui.py` `/api/ask` `:1597`, `/api/confirm` `:1595`, `/api/remote*` `:1630-1635`; `Console.submit` `:825`; `_work` `:850+` | `assistant.ask`; Remote view | loopback + console credential (`cluster.py:987-990` `authenticate(..., "console")`, owner cap); one running job at a time `ui.py:838-840` |
| Phone PWA | `jarvis/phone.py` `ask` `:1193`, `start_session` `:1113` → `hub.remote_open` `:1123`; routes `:1460-1487` | ask pipeline; screen/camera session | paired-phone auth (`phone.json` exists; contents not read), `phone.lock`, camera consent |
| Node → MAIN HTTP | `/v1/ask` `jarvis/cluster.py:1104-1120`, `/v1/control` `:1078-1100`, `/v1/action` `:1077` | enqueue `ask` job; `Hub.dispatch` | node capability from `cluster.sqlite3`; nodes barred from MAIN target (except transfer) and from terminal (`:1090-1100`); `/v1/action` owner-only |
| Hub ask worker | `jarvis/cluster.py:835-870` (`_ask_once`, `_console_ask`) | `request_from(job["requester"])` then handler | none |

### 2.2 Dispatch and execution

| Path | Evidence | Behavior |
| --- | --- | --- |
| Hub dispatch, MAIN target | `jarvis/cluster.py:581-592` | `node == self.main` → `execute_local` immediately. Rejects `remote`/`camera` kinds only. Covered by `tests/cluster/test_cluster.py:259` |
| Hub dispatch, node target | `jarvis/cluster.py:593-633` | capability + online + not-busy + signed-code-match checks, `broker.enqueue(node, main, kind, payload, timeout)` **without a caller idempotency key**; waits; timeout/uncertain text says "do not automatically retry" |
| Local effect switch | `jarvis/cluster.py:409-435` (`execute_local`) | `desktop`→`remote.run_here`; `storage`, `transfer`, `remote`, `camera`, `locate`, `grokbot` |
| Module-level `dispatch` | `jarvis/cluster.py:1144-1153` | node refuses; main uses in-process Hub from `_HUBS` or an owner-token HTTP client |
| `Remote.call` | `jarvis/remote.py:211-270` | target = explicit device or `ORIGIN` or local. **target == local → `run_here` directly, no Hub** (`:222-229`; main passes `config/state` so app_ui approvals work). Node + remote target → `Client /v1/control` body exactly `{node, kind, payload}` (`:231-236`). Main + remote target → `dispatch()` (`:237-240`). Cluster disabled → local or SSH/tunnel (`:255-266`) |
| SSH fallback | `jarvis/remote.py:244-250,266` | when a node is **offline pre-dispatch**, MAIN runs `python -m jarvis desktop …` on it over SSH, bypassing node agent and job queue. Timeouts are not retried by this path |
| `run_here` | `jarvis/remote.py:106-147` | direct sinks: `control.run` `jarvis/control.py:660`, `app_ui.run` `jarvis/app_ui.py:402`, `terminal.run` `jarvis/terminal.py:643`, `sysintents.run` `jarvis/sysintents.py:758`, `actions.py` (spotify etc.) |
| Terminal gate | `jarvis/remote.py:129-137` | `gates.hard_deny` re-judged with the local config before `terminal.run` |
| Other effect sinks | `transfer.run_here` `jarvis/transfer.py:280`; `remote_view.run` `jarvis/remote_view.py:1380`; `camera.run` `jarvis/camera.py:711`; `locate.run_here` | reached via `execute_local` (node job or MAIN in-process) |
| Brain tools | `jarvis/actions.py:339-364` (`DesktopTools.call` → `remote.call`); `jarvis/chatgpt.py:294,906,922,977`; `jarvis/tools_mcp.py:70-74` | tool calls from a **spawned model process** reach `Remote.call` / local tools |
| Pipeline direct executor | `jarvis/pipeline.py:405-425` (`remote.call(command["device"], action, value)`); approved commands `run_confirmed` `:319+` | strict-parser path; INFLIGHT merge window dedupe only |
| Approvals | `jarvis/confirm.py:98` (`park`), `:171` (`approve`); `jarvis/assistant.py:714-760` (`confirm_pending`) | approval is bound to origin and a hash; it is **not** bound to an epoch or target-specific execution ID. Approval store is `history.sqlite3` (via `History`) |
| Node job verification | `jarvis/cluster.py:1362-1393` (`_untrusted`), `jarvis/deploy.py:191-215` (`_job_bytes`, `sign_job`, `verify_job`) | signature covers `{id,node,kind,payload,issued}` only. `sign_job` stamps `issued = now` **when the job is handed to the node** (`cluster.py:1053-1055`, `deploy.py:203`), so original request age is not preserved. Replay set `cluster-jobs-seen.json` forgets after 3600 s; skew window `JOB_SKEW = 600` (`deploy.py:187`). Pinned test: `test_signed_job_fields_do_not_include_authority_or_original_issue_time` |
| Node worker | `jarvis/cluster.py:1263+` (`once`), `:1430` (`run`) | single poll/exec loop per node; result outbox `cluster-outbox*.json` (crash resend never re-executes) |
| Model task runner | `jarvis/runner.py:126-154` | `Popen(codex/claude …, cwd=workspace, env=subscription_env())`; refused on node by `is_node`; no per-child authority |
| Model CLI spawns | `jarvis/chatgpt.py:239-246`, `jarvis/fastlane.py:187-194`, `jarvis/knowledge.py:1376-1386`, `jarvis/claude_auth.py:38-76` | children inherit `subscription_env()` minus API keys/`CLAUDE*`; nothing strips the state dir, so a child running as the same user can read `cluster-owner.token`. `jarvis/__init__.py:11-80` audit hook blocks these spawns **only** under tests / `JARVIS_NO_MODELS=1` |

### 2.3 Managed launchers (from repo sources only)

Whether each is installed or enabled on any host is **UNKNOWN** (no `systemctl`/`schtasks` queried).

| Launcher | Source | Starts | Effect outside Hub? | Existing guard |
| --- | --- | --- | --- | --- |
| Generic installer | `jarvis/autostart.py:20-24,52-64,100-152` | systemd user unit / Windows Startup `.vbs` / macOS LaunchAgent | n/a | none |
| `jarvis-cluster` | `jarvis/cluster_cli.py:247-266` | `cluster run` → main Hub + console, or `NodeWorker` | yes | `cluster-main.lock` / `cluster-node.lock` |
| `jarvis-cluster-tunnel` | `jarvis/cluster_tunnel.py:95-107,194,257-277` | `ssh -R` (main) / `ssh -L` (node) | opens hub path | tunnel lock |
| `jarvis-clipboard` | `jarvis/clipsync.py:1229-1257` | clipboard watcher + dropzone | file drops (`dropzone.py:775-780`) | `clipboard.lock`, pid, STOPPED marker |
| `jarvis-voice` | `jarvis/daemon.py:828-926` | `voice listen` | `ask(...)` | `voice.lock`, pid |
| Overlay `.vbs` | `jarvis/overlay.py:309-372` | overlay; Hub on main, worker on node | yes | Windows mutex |
| Node agent launcher | `jarvis/cluster_setup.py:443-470` | `cluster run` | yes | node lock |
| Lan Mouse tasks/units | `jarvis/sharedinput.py:313-316,358-426,536-580`; `rest.py:35`; CLAUDE.md "Lan Mouse" | `lan-mouse daemon`, elevated Windows tasks, guard | shared mouse/keyboard | none besides already-running check; **unit text for `lan-mouse*.service` is not in the repo → UNKNOWN** |
| Watchdog | `jarvis/release.py:324-352` | `systemd-run` `jarvis-code-guard-*`, restarts `jarvis-cluster` | restart | none |
| `rest.sleep/pause/wake` | `jarvis/rest.py:35,106-215` | stops/starts own units, `suspend-then-hibernate` | power | allow-list `OWN_UNITS` |
| Knowledge timer | `jarvis/knowledge.py:1545-1590` | daily task/timer | command not read → **UNKNOWN** | UNKNOWN |
| Scripts / configs | `start.sh:13-23`, `start.bat:15-20`, `.claude/launch.json:4-14`, `grok-check.sh:5` | `voice start` + `app`; `ui` ports 7777/7778; Grok check | a Hub if config says main | locks above only; `grok-check.sh` effect **UNKNOWN** |
| Extra console/overlay on a main | `jarvis/cluster_proxy.py:216-262`, `jarvis/ui.py:1784` | creates a second `Hub` unless one already holds the lock / owner token probe answers | yes | `cluster-main.lock` + `/v1/status` probe |
| Phone server | `jarvis/phone.py:1494-1507`, started `jarvis/cluster_proxy.py:260` | main-only listener | screen/camera sessions | `phone.lock`, `Store.on()` |
| Grok tunnel | `jarvis/grokbot.py:753-770` | Windows reverse tunnel when cluster is off | network | relay-port ownership |

### 2.4 Authority bypass boundaries that exist today

1. **Config-only role** (§1): a stale or hand-copied config makes two PCs believe they are MAIN.
2. **MAIN-local sinks skip Hub:** `Remote.call` target==local (`jarvis/remote.py:222-229`) and
   `Hub.dispatch` main target (`jarvis/cluster.py:586-592`) run effects with no authority tuple.
3. **Standalone CLI:** `jarvis desktop …` constructs `Remote` directly (`__main__.py:404-410`) and
   does not need a running Hub; `jarvis sleep/lanmouse/control/clipboard` likewise.
4. **Alternate state dir / second process:** locks and tokens are per `--state` directory
   (`cluster.py:898`, `cluster_store.py:35`); a different directory defeats them.
5. **Owner token readable by same-user processes** and not stripped from spawned model CLIs (§2.2).
6. **Spawned agents:** brain/runner child processes call tools through `DesktopTools`/MCP
   (`actions.py:339`, `tools_mcp.py:70`) and keep working after any future demotion; a CLI's own
   shell/edit tools are outside JARVIS entirely (design limit in DESIGN.md/MAIN-HANDOFF §2).
7. **Offline SSH fallback** runs the managed CLI remotely, bypassing the node agent and any node-side guard.
8. **Request identity:** job `issued` resets at hand-off; `/v1/control` and `/v1/action` carry no client
   idempotency key; `Hub.dispatch` never passes `request_id` to `broker.enqueue` (the broker supports it,
   `cluster_store.py:216-233`). Node timeouts are reported as uncertain, never auto-retried (`cluster.py:620-628`) — a good legacy property to keep.
9. **Approvals** are not epoch-, target- or operation-bound (§2.2).
10. **Unmanaged old processes** (an old checkout, hand-made units) cannot be fenced by markers (MAIN-HANDOFF §2 limit).

## 3. State and provider ownership (names and metadata only)

Directory `.jarvis/` (gitignored; mode and size observed with `ls`; no bodies read). Whether
a given file is also present on the Dell or Oracle is **UNKNOWN**.

| Item | Observed / source | Owner today | Plan placement |
| --- | --- | --- | --- |
| `memory.sqlite3` (~1.5 MB), `vault.sqlite3` (~1.7 MB), `history.sqlite3` (~160 KB, also approvals) | `.jarvis/`; `jarvis/memory.py`, `vault.py`, `history.py`, `confirm.py` | MAIN PC | PC-only personal state; never on Oracle |
| `cluster.sqlite3` (~7.3 MB: nodes, tokens, jobs) | `jarvis/cluster_store.py:42` | MAIN PC | split: Oracle gets metadata authority; job/receipt journals stay per PC (MAIN-HANDOFF §1 row `cluster_store.py`) |
| `cluster-owner.token`, `code-signing.key` (+`.pub`), `chrome-extension-token`, `phone.json`, `camera.json`, `locations.json` (mode 0600) | `jarvis/cluster_store.py:35`, `deploy.py`, `phone.py`, `camera.py`, `locate.py` | MAIN PC | credentials stay local; only names/modes were read |
| `cluster-node.json`, `cluster-jobs-seen.json`, `cluster-code-version.json`, `cluster-outbox*.json` | `jarvis/cluster.py`, `deploy.py` (node state; not present in the main's `.jarvis/`) | each NODE | PC-local receipt journal seed |
| `tasks.sqlite3`, `runs/`, `plans/`, `knowledge/`, `inventory/`, `eval/`, `code-release/` | `jarvis/tasks.py`, `runner.py`, `knowledge.py`, `inventory.py`, `release.py` | MAIN PC | workflow/journal state: PC-only, replicated later (P6) |
| `events.jsonl` (~345 KB), `logs/`, `brain-budget.json` | `jarvis/events.py`, `credits.py` | MAIN PC | local observability; cloud gets numeric metadata only |
| Locks and pids: `cluster-main.lock`, `clipboard.{lock,pid}`, `voice.{lock,pid}`, `phone.lock`, `consolidate.lock`, `code-release.lock`, `lanmouse-guard.json{,.lock}` | `.jarvis/` | per process | lock model is per-PC only (§2.4) |
| Browser/agent profiles: `browser-profile/`, `agent-browser-profile/`, `app-window/`, `agent-browser-frame.jpg` | `.jarvis/` | MAIN PC | private; never moved |
| Provider sessions | Codex: `$CODEX_HOME` or `~/.codex` (`auth.json`, `config.toml`, `sessions/`); Claude: `$CLAUDE_CONFIG_DIR` or `~/.claude` (`.credentials.json`) — `jarvis/credits.py:179-180`, `chatgpt.py:67-68`, `claude_auth.py:56`, `usage.py:139`, `gates.py:691` (protected names) | each PC's user account | credentials location only recorded; **login state/quota on the Dell and Oracle: UNKNOWN** |
| Config | `jarvis.config.json` (secrets file name `secrets.json` referenced in code) | each PC's checkout | role authority must leave this file (P2) |

Oracle resources, network reachability, Tailscale state, Windows service/Startup contents,
OS/arch of the Dell, spare RAM/disk and latency baselines: **UNKNOWN** — P0 "measured baseline"
for these needs owner-authorized read-only probes later; none were run.

## 4. Test coverage map and gaps

Existing coverage (counts are `def test` per file; all use fakes, tests disable models):

| Area | Tests |
| --- | --- |
| Roles / `set_main` | `tests/cluster/test_cluster_roles.py` (3), `tests/cluster/test_fleet_contracts.py` (other lead's P1 facade parity) |
| Hub, job queue, dispatch main-target, offline/busy | `tests/cluster/test_cluster.py` (56; main target `:259`, busy `:709`), `test_cluster_hardening.py`, `test_audit_cluster.py` |
| Job signing, freshness, replay | `tests/cluster/test_job_signing.py` (5) |
| Console/proxy/main lock | `tests/cluster/test_cluster_proxy.py`, `test_cluster_setup.py` |
| Remote.call, kinds, lifecycle, chaos, view | `tests/remote/test_remote.py`, `test_remote_kinds.py`, `test_remote_view.py` (38), `test_remote_chaos_*.py` |
| Voice main-only | `tests/voice/test_main_only_voice.py` (12) |
| Phone | `tests/mobile/test_phone.py` (81), `test_phone_e2e.py` (5) |
| Approvals / gates | `tests/security/test_confirm*.py`, `test_hard_deny.py` (21), `test_gates_redteam.py` |
| Sleep/guards | `tests/runtime/test_rest.py` (14), `tests/remote/test_lanmouse_guard.py` (34) |
| Baseline pins (new) | `tests/runtime/test_harness_baseline.py` (4): stale-config dual-MAIN, local effect without Hub, node envelope fields, signed-job fields |

Gaps (none of these are tested today):
- No durable-mode / stale-epoch / downgrade / alternate-`--state` test (nothing exists to test).
- No real multi-process test of two PCs claiming MAIN, or of reboot with stale config.
- No effect-sink inventory test (a registry that fails when a new sink bypasses a guard).
- No test that spawned agent tools lose rights after demotion; no unmediated-CLI classification.
- No end-to-end test of idempotent retry across targets, nor an original-issuance-age test.
- No launcher-inventory test for unit/Startup text (units are generated by `autostart.py`; hand-made Lan Mouse units are not in the repo).
- Real Windows/Oracle/phone behavior: none; tests are fake-backed.

## 5. P1 facade contracts and acceptance gates (future requirements)

P1 = behavior-preserving facades only (ACTION-PLAN P1). Nothing here changes who may act. The
fleet-facade lead owns `jarvis/fleet/` production code; this file only states the gates.

| Facade seam | Contract (proposed) | Parity gate (existing behavior) |
| --- | --- | --- |
| Topology (`jarvis/fleet/legacy_topology.py`, re-exported by `jarvis/topology.py`) | separate **local device**, **configured main**, **request origin**, **approval station**, **execution target** as distinct values; legacy source is config | identical results/messages for `enabled/identity/is_node/endpoint`, existing imports and tested patch points preserved; re-export global-replacement limits documented in P1-IMPLEMENTATION.md (`tests/cluster/test_fleet_contracts.py`) |
| Effect admission | one function every sink would call with a caller-supplied immutable context; in P1 it is **legacy-permissive** and records nothing | all §2 paths still behave as listed; adding it must not change `Remote.call`/`Hub.dispatch` outputs |
| Dispatch/executor facade | `submit(kind, payload, target, *, origin, approval)` wrapping `Hub.dispatch`/`Remote.call`; names the execution target explicitly; returns the legacy result | `tests/cluster/test_cluster.py`, `tests/remote/*` unchanged; node envelope stays `{node, kind, payload}` until P2 |
| Provider lane facade | wraps `runner`/`chatgpt`/`fastlane` spawns; reports class (mediated vs legacy CLI); **no strict-fencing claim** for legacy CLI | `tests/runtime/test_runner.py`, `tests/intelligence/test_inference*.py` unchanged |
| State facade | read-only accessors naming state owner per §3; no file moves | no path or schema change; `.jarvis/` listing unchanged |
| Phone/console | read-only status view of roles + distinct origin/target/approval; mobile disconnect never changes MAIN or replays input | `tests/mobile/*`, `tests/web/*` unchanged |

P1 acceptance gate (all required): existing targeted suites pass; the full suite passes once at
integration; no new port, daemon, state move, config key or credential read; `git diff` shows
facade wiring only and is revertible; this baseline's pins still pass **unless** a pin's gap is the stated goal
(then the pin changes in the same patch with an explanation); every sink in §2 is either wrapped
or listed as an explicit exception with owner.

Not P1 (do not start from this baseline): durable mode store, Oracle authority, signed execution
envelopes, receipts, elections, gateway deployment, failover (P2–P4+).

## 6. Test runs recorded for this file

- `.venv/bin/python -m unittest tests.runtime.test_harness_baseline` → 4 tests OK.
- With `tests.cluster.test_cluster_roles test_job_signing test_cluster test_cluster_hardening test_cluster_proxy`,
  `tests.voice.test_main_only_voice`, `tests.remote.test_remote`: 143 tests, OK (21 s).
- Full suite not run by this lead (parent runs it once at integration).

## 7. Risks and next stage

- Line numbers shift while parallel edits continue; they were captured 2026-11-11.
- One child's findings (launcher table) were spot-checked against source for the key lines
  (`cluster.py`, `remote.py`, `deploy.py`, `cluster_cli.py`); the rest (e.g. `knowledge.py`, `overlay.py`, `release.py`) are child-reported and not individually re-read.
- Config was not read; per-host role/`local_device` values remain UNKNOWN.
- Next: P1 facades by the fleet lead against §5, then P2 (stable installation identity, durable mode
  guard outside per-command `--state`, target-bound execution IDs with immutable issuance time).
  P0 "measured baseline" (Oracle/Dell resources, latencies) remains unmeasured; the implementation request permits relevant read-only probes, but live machine access must first be established without reading credentials.

Parent integration: all 43 P0/P1 characterization/facade tests pass together. The earlier full
suite passed 2999 tests; P0 tests were independently rerun on inventory completion. Do not
infer Oracle/Dell measurements or production authority guarantees from these results.
