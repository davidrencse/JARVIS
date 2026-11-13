# Continuity contract

*Operational rules the isolated continuity prototype and the harness tests pin. Not a production cutover.*

Design intent is [continuity-design.md](continuity-design.md). Module behavior is [continuity-runtime.md](continuity-runtime.md). Fleet invite and role publication are [fleet-api.md](fleet-api.md).

`jarvis/continuity/` is an isolated prototype. Importing it does not bind a socket. `ContinuityGateway.serve` is the only listener, and only when called. `Remote.call`, `NodeWorker`, `Hub.dispatch`, and `execute_local` stay unfenced unless the opt-in launch hooks below are installed. Those hooks default open. They are not a fence.

## Deployment decisions

- One canonical config writer: `config_lock.config_mutation`, `patch_config`, and `save_delta`, on `<state>/fleet-manage.lock`. Fleet, settings, and `jarvis cluster configure|main|init|invite|enroll|join|leave` are domain adapters on that lock. The lock is one machine. A hand edit does not take it. It is not a lease.
- `settings.write` patches only the keys it was asked to change, through `patch_config`. `cluster.main_device` is accepted only as the sole change, and only through `cluster_cli.set_main`. Combining it with other keys raises `ValueError` and saves nothing. Success records the overlay only when `ok`, `mutated`, and `main` all match the requested device. A pending or failed handoff returns `saved: []` and does not rewrite the role.
- `set_main` with no bound handoff refuses and does not rewrite `cluster.main_device`. Two config files can disagree: `identity()` trusts the file it was given.
- `cluster.save_json` writes `cluster-node.json`, not the managed user config.
- Roles are descriptive. They grant nothing. `Remote.call` uses `Roles.execution_target`. A local sink can skip the Hub.
- Signed job fields still do not carry authority id or the original issue time. That gap stays until a ticket does.
- `legacy_topology` is a facade over today's unfenced paths. It is not v2 admission.

## Five facades

Names `integration.PEER_FACADES` checks. Adding a method only so the name check passes, without the behavior, makes `compatible` false.

| Facade | Type | Methods |
| --- | --- | --- |
| gateway | `ContinuityGateway` | `status`, `renew`, `release`, `acknowledge`, `transition_request`, `transition_readiness`, `transition_step`, `consume`, `admission`, `relay` |
| ingress | `MobileIngress` | `pair`, `authenticate`, `status`, `submit`, `operation`, `manage_main_preview`, `manage_main_confirm` |
| runtime | `MainRuntime` | `start`, `readiness`, `renew_once`, `require_authority`, `begin_drain`, `release_before_suspend`, `invalidate_after_resume`, `stop_new_admission` |
| executor | `ExecutorGuard` | `accept`, `fence`, `reconcile`, `stop_interactive` |

`GatewayPort`, `AuthenticatedPort`, and `SignedAuthority` are not interchangeable. `AuthorityStore` (writer), `CachedAuthority` (cache), and `MainRuntime` (lease holder) are not one type. `ExecutorGuard` and `AuthorityStore.consume` protect different windows: the journal fences the local effect; the store consumes the admission. `MobileIngress` SQLite and the gateway's RAM relays do not share a file.

`integration.PERSONAL` and the gateway's forbidden-key set differ on purpose. Do not drop `secret` from `PERSONAL` to make a stand-in identity pass. The old `LoopbackGateway` / `integration.MobileIngress` doubles are gone. Do not bring them back.

Launch hooks (`configure_production` and the opt-in installers) are live helpers, default open. Do not delete them to "simplify" and do not treat them as the production fence.

## Effect-sink inventory

`tests/runtime/test_harness_boundaries.py` asserts these sets. A new or removed `remote.ACTIONS` entry must be classified here and in that test. The sets are disjoint and their union is `remote.ACTIONS`.

Read-only: `list_apps`, `observe`, `input_info`, `screen`, `system_status`, `weather`.

Host effect: `open_app`, `open_url`, `open_page`, `focus_window`, `close_app`, `media`, `volume`, `scroll`, `browse`, `lan_mouse`, `terminal`, `clipboard`, `computer`, `spotify`, `app_ui`, `brightness`, `screenshot`, `workspace`, `lock_screen`, `reminder`, `bluetooth`.

Hub kinds `cluster.validate_action` accepts: `desktop`, `inventory`, `grokbot`, `storage`, `transfer`, `remote`, `camera`, `locate`. `shell` is refused (`Nodes execute only`). `cluster.IN_PROCESS_ONLY` is a subset of those kinds.

Direct intents map through `pipeline.DIRECT` except `spotify_play`, which stays on Grok Bot on purpose. Non-direct executors: `agent_task` → `grokbot`, `answer` → `brain`, `vault_write` → `vault`, `build` → `orchestrator`.

Every action has exactly one argument name in `remote.ARG`.

## Node content routes

A node token, including one with `console` or `ask`, is denied:

- `/api/history`, `/api/history/commands`, `/api/events`, `/api/tasks/<id>`
- `/api/memory`, `/api/settings`, `/api/files`

POST `/api/ask` without capability `ask` is denied. Allowlists stay allowlists. Do not open these routes to make a node console useful.

## P2 rules that stay

- `confirmation_station(origin)` is the approval station. Phone pairing is not it.
- `Failover.ask` may repeat a pure model request only before a tool call has started. A started tool call blocks that replay. This is provider failover, not continuity replay.
- `knowledge._llm_answer` uses `CLAIMS_ONLY_FLAGS`. Answers from that path are claims, not tool authority.

## What the prototype must keep failing closed

- Consume is the target plus a MAIN signature. `transition_step` is owner-proof only, including `grant`. A candidate must not grant itself.
- `AuthenticatedPort.consume` requires `ticket_signature` and refuses a signature over `MAX_SIGNATURE` before I/O. `ExecutorGuard.accept` still calls `consume` without that signature. Wiring the guard at that port fails closed. Do not drop the signature to make the call pass.
- Gateway `not_found` is 404. Unknown store codes are 503 with no message. Ingress collapses an unknown or unauthorized gateway code to `authority_unavailable` (503).
- Windows `remaining()` is 0 on purpose when the clock cannot prove uptime. Do not invent a boot clock.
- `MainRuntime` never calls `make_main_now`. Failback waits at least 30 seconds of stability.
- Phone CSRF, passkey confirmation, and `accept_gateway_phone` stay unimplemented. Do not trust a cookie or `X-Forwarded-For` from a gateway hop as a phone claim.
- A metadata relay is not an admission. `effect_authorized` on that path stays false.
