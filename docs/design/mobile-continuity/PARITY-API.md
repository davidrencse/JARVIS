# API parity — console, phone, fleet, cluster auth

Date: 2026-11-11. Read-only audit of the working tree on
`implementation/ui-parity-checkpoint-20261111` (HEAD `845b558` plus existing dirty
files). No code, test, config, state, or vault edit. No server, no live DB, no token
contents, no full suite.

This is the frontend compatibility contract for routes that already work. It is not a
screen catalog (see the fleet UI handbook and `FLEET-API.md` for copy and layout). It is
not a production-ready claim. Parent suite `3434` passed before the last Settings patch
(`IMPLEMENTATION-PROGRESS.md`); that run does not cover every case below. Open
consolidation bugs remain. Future MAIN lease, gateway, safe handoff, distributed
compute, and C3 vault-primary are not built. Obsidian C2 is the live writer; SQLite
stays the personal source. Battery fields are Linux/Windows fixtures, not a measured Dell.

`FLEET-API.md` is still the fleet body reference. Where this file and that one disagree,
this file follows the current source. Two notes in `FLEET-API.md` are stale: CLI
`configure` / `main` / `invite` now take `fleet-manage.lock`, and a role flip during
signing-key generation is refused before a credential is written.

## Verdict for a new frontend

Keep every route below. Do not remove a write, change a status code, or invent a MAIN
handoff. Switch fleet failures on `code`. Treat a Settings refusal as a plain
`{"error": "..."}` with no `code`. A paired phone is not fleet admin. A node token is
not the owner.

## Auth boundaries

Four callers. They do not share a credential.

| Caller | Proof | What it may call | What it must not |
| --- | --- | --- | --- |
| Owner console on the main | Loopback `Host` in the handler allow-list, `Sec-Fetch-Site` `same-origin` or `none` (or absent). POST also `X-JARVIS: 1`. No bearer. `jarvis/ui.py:1409-1413`, `:1677` | Every `/api/*` on that process, including fleet writes when this PC is the configured main | Cross-site, wrong Host, POST without `X-JARVIS` → 403 before the body is acted on |
| Node console (proxy) | Same loopback gates locally, then the node's bearer is forwarded with `X-JARVIS-Main` equal to the hub's main. `jarvis/cluster_proxy.py:166-179`, `jarvis/cluster.py:1024-1033` | `NODE_API_READ` GETs; POST `/api/ask`, `/api/listen`, `/api/confirm` only with capability `ask`. Own job at `GET /api/jobs/<id>` | Settings, fleet, files, screen, memory, history, events, plans, uploads, Grokbot commands. 403 `Nodes can't use …` |
| Node agent on the hub | `Authorization: Bearer <node token>` and `X-JARVIS-Main` equal to `hub.main`. `jarvis/cluster.py:516-526` | `/v1/poll`, `/v1/result`, `/v1/redeem`, `/v1/code`, `/v1/remote/frame`, `/v1/remote/input` when the capability matches. `/v1/control` with `files` only for `kind=transfer` aimed at the main | `/v1/action` (owner only). Camera, locate, and remote-session **start** over HTTP (in-process only, even for the owner token). Write-runs (`/v1/ask` `write:true`) |
| Paired phone | Port 7787 only. Host is the tailnet name or loopback. `X-Forwarded-For` is a tailnet address `whois` knows. `Tailscale-User-Login` matches. Cookie `__Host-jarvis_phone` matches that device and login. POST also `X-JARVIS-Phone: 1` and a JSON object ≤ 256 KiB. `jarvis/phone.py:1-15`, `:1442-1497` | The phone table below | Any console path (404, no fall-through). Any fleet write (404). Funnel or an unreadable serve config → 503 for every request |

Owner token on the hub (`hmac` against the hub owner secret) is the main's own windows,
not a node. It reaches the full console API. It still cannot start `remote`, `camera`,
or `locate` over `/v1/action` or `/v1/control` (`jarvis/cluster.py:1118-1130`).

Node field cutting: `GET /api/overview` keeps `version, now, cluster, devices, routing,
models, brain, live, voice, tools`. `GET /api/confirm` keeps `pending, grants`. Other
allowed reads are not field-cut. `jarvis/cluster.py:46-57`, `:398-403`.

Local-only on a node proxy (never forwarded): `GET/POST /api/voice/setup`,
`GET /api/voice/devices`, `GET/POST /api/peripherals`, `POST /api/overlay`.
`jarvis/cluster_proxy.py:87-160`.

## Error envelopes the client must branch on

| Source | Status | Body | Cache |
| --- | --- | --- | --- |
| `FleetError` | the exception's status (400, 404, 409, 500) | `{error, code, retryable, step?, …}` | `no-store` (`jarvis/ui.py:1542-1546`) |
| Other console `ValueError` (Settings, bad JSON) | 400 | `{error}` only. No `code` | `no-store` |
| Console `LookupError` | 404 | `{error}` | `no-store` |
| Console anything else | 500 | generic text. `OSError` does not include the path. `RuntimeError` text is passed through | `no-store` |
| Phone `Reply` | the status it carries | `{error, …extras}`. No fleet `code` | `no-store` |
| Hub `PermissionError` | 403 | `{error}` | handler default |
| Hub `NodeOffline` | 409 | `{error, code:"node_offline"}` | handler default |
| Hub `ValueError` / `TypeError` / `OSError` / `RuntimeError` | 400 | `{error}` | handler default |

Oversized console JSON/audio (`Content-Length` above `MAX_AUDIO`, 45 s of float32) is
413 `{error:"Too large."}`. A declared length past the drain cap can still reset the
socket instead of delivering 413 (`jarvis/ui.py:1415-1427`). Do not treat a reset as
success. This is the open upload finding in `AUDIT-SUMMARY.md`, not re-run here.

## Unsafe Settings MAIN path — blocked

The old control posted `{changes: {cluster.main_device: "<id>"}}` with no `confirmed`
flag and rewrote the file. That HTTP write is closed.

| Layer | Now | Evidence |
| --- | --- | --- |
| `POST /api/settings` | `settings.write` raises `ValueError` before any disk write when any key is `cluster.main_device`. Message: `Moving MAIN is unavailable until safe handoff is implemented. See Connections for readiness.` Console maps that to **400** `{error}` and does not save. `jarvis/settings.py:261-262`, `jarvis/ui.py:1740-1746` | **Verified this session.** `tests.runtime.test_settings.SavedClusterTests.test_a_saved_main_shows_before_the_restart_and_no_broker_is_built`: exception matches `safe handoff`, process config stays `zenbook`, file stays `zenbook` |
| Settings page | `HIDDEN` includes `cluster.main_device`. Machine rows render the sentence `MAIN transfer unavailable. See Connections for readiness.` They do not emit `data-ask-main` or `data-make-main`. A leftover click on those attributes only toasts; it does not call `save()`. `jarvis/web/settings.js:11`, `:193`, `:273-277` | **Source.** No DOM test asserts the absent button. Not browser-checked this session |
| Fleet execute | `POST /api/fleet/main-transfer` is always **409** `code:"blocked"`, `mutated:false`. It does not write `cluster.main_device`. Preview is **200** `{status:"blocked", mutated:false}` | **Verified this session.** `ConsoleHTTPTests.test_transfer_routes_are_preview_or_409` |
| `GET /api/settings` | Still returns `cluster.main_device` as a `choice` field whose choices are device ids. `jarvis/settings.py:21-24`, `:162-170` | Same Settings test reads the field back as `zenbook` |

Frontend checklist for this path:

- Do not render a control for `cluster.main_device`. A generic field renderer will show one, and Save will 400.
- Do not POST that key. Do not send `confirmed:true` to bypass it. Confirmation is only for `LOOSENS` (`rules.confirm=never`, `terminal.sandbox=false`, `lan_mouse.off_when_apart=false`).
- Do not shell out to `jarvis cluster main`. That CLI still rewrites the key under the fleet lock (`jarvis cluster main` is a managed writer, `jarvis/config_lock.py:3-6`). It is not safe handoff.
- Point "Make main" at `POST /api/fleet/main-transfer/preview` and show `missing` and `why`. Keep the execute control disabled. The execute route stays for the contract; calling it changes nothing and returns 409.
- `GET` still listing the field is not permission to write it.

`settings.write` does not take `fleet-manage.lock` (`jarvis/config_lock.py:15-18`).
`cluster.tunnel` is still a normal Settings save: disk updates, the running process
keeps the old tunnel until restart (`jarvis/settings.py:287-288`). A save that lands
before a fleet replace is `config_changed` (retryable). A save in the instant between
the compare and the rename can still be lost. That window is documented, not closed,
and it is not a MAIN write.

## Fleet compatibility checklist

Caller: owner console on the main, `can_write:true`. Phone and node tokens follow the
auth table. All `/api/fleet*` replies are `Cache-Control: no-store`.

| Route | Body | Success | Failure the UI must handle | Side effect |
| --- | --- | --- | --- | --- |
| `GET /api/fleet` | none | 200 inventory. `audience:"owner"`. Devices include hostname, address, `invite_expires`, `code_ok`, `active_jobs` | 403 wrong host / node token. 500 generic if the builder throws (no path in an `OSError`) | Read-only sqlite. Does not create `cluster.sqlite3` |
| `GET /api/fleet/vault` | none | 200 numeric archive + local git. No remote URL, path, filenames, or `last_error` | same | Local `git` with fixed args, 5–8 s, 15 s cache. No network |
| `POST /api/fleet/preview` | `{node, profile?, os?, addresses?, capabilities?, label?}` | 200 `{mutated:false, …}`. Commands contain the literal `<INVITE CODE>` | 400 `bad_*` / `unknown_fields`. 409 `main_not_allowed` | None. Does not call `invite()` |
| `POST /api/fleet/invite` | `{request_id, node, profile?, os?, label?, addresses?, capabilities?, acknowledge?, replace_pending?}` | 200 `{ok, code, secret:true, step:"join", …}`. `code` is shown once | See codes below | Device entry, backup, receipt file, one SSH key line, one hub credential. Bus `fleet.changed` is `{action, node}` only |
| `POST /api/fleet/revoke` | `{node, confirm}` and `confirm` must equal `node` | 200 `{ok, was_enrolled, ssh_key_removed, active_jobs_cancelled, step:"invite"}` | 400 `confirm_required`. 409 `main_not_allowed`, `not_main`, `cluster_off` | Deletes credential and SSH key. Clears that node's receipts. Idempotent if not enrolled |
| `POST /api/fleet/capabilities` | `{node, capabilities, acknowledge?}` | 200 `{ok, changed, credential_rotated:false, added?, removed?}` | 404 `unknown_node`, `not_enrolled`. 400 `capability_ack_required` (`needs_ack`) | `Broker.set_capabilities` only. Token unchanged. `camera` still needs `jarvis camera allow on` on the node |
| `POST /api/fleet/main-transfer/preview` | `{target}` | 200 blocked preview | 400 `bad_node` / `unknown_fields` | None |
| `POST /api/fleet/main-transfer` | `{target}` | never 200 | **409 `blocked`**, `mutated:false`, plus `missing`, `readiness`, `phases`, `risks` | None |

`request_id` is 8–64 of `[A-Za-z0-9_-]`. Unknown JSON keys are 400 `unknown_fields`.
Empty or non-object bodies are 400 (console `ValueError` or `bad_body`).

Invite codes the client switches on:

| `code` | HTTP | Retry |
| --- | --- | --- |
| `bad_body` `bad_node` `bad_request_id` `bad_profile` `bad_os` `bad_address` `bad_label` `bad_capabilities` `bad_acknowledge` `bad_replace` `unknown_fields` `capability_ack_required` | 400 | Fix the body. Same `request_id` is fine if no receipt was stored |
| `main_not_allowed` `cluster_off` `not_main` `node_enrolled` `invite_pending` `already_issued` `tunnel_forward` `profile_mismatch` | 409 | `already_issued`: new `request_id`, and `replace_pending:true` to drop an unused pending invite. Do not ask the server for the old code |
| `sshd` `busy` `config_changed` | 409 | `retryable:true`. Same `request_id` when the receipt was dropped |
| `invite_failed` | 409 or 500 | `retryable` is the authority. `false` or `recover:"revoke"` means revoke that node before a new id. Do not claim the invite was atomic |
| `config_unreadable` `state_unwritable` | 500 | Not a client bug |

`config_changed` is new relative to the printed code list in `FLEET-API.md`. Show it as
"the config changed underneath; try again." Do not retry blindly in a loop.

Profile defaults: `pc` → desktop, inventory, grokbot, files. `headless` and `cloud` →
inventory only. Anything else needs the same ids in `acknowledge`. Cloud
`main_eligible` is `false` and `main_candidate` is `never`. Do not call a cloud node a
gateway.

Phone projection of the same inventory (`GET /phone/api/fleet`): `audience:"phone"`,
`can_write:false`, `cluster.local:null`, no hostname, address, invite expiry, `code_ok`,
or `locate`. `status` **does** include `invite_pending` and the other link states, and
`step` is present. Write actions are `blocked` with no `path`. No preview action. The
comment in `phone.PhoneServer.fleet` that says "no invite state" is wrong; the test and
`inventory()` are right (`jarvis/fleet_management.py:347-349`,
`tests/mobile/test_phone_fleet.py`).

Support values on `actions[]`: `supported` (button), `guide` (text), `blocked` (disabled
plus `why`), `unsupported` (`cloud_provisioning` only). On a node console or with the
cluster off, owner writes are `blocked` in the payload and 409 `not_main` or
`cluster_off` if called anyway.

## Phone routes

Shell files are the fixed `SHELL` map. Everything else is 404. Methods other than GET
and POST are 405. Pairing is `POST /phone/pair` and `POST /phone/pair/status` with
`{code}` (8 characters; spaces and dashes ignored).

| Route | Body / query | Success | Failure | Side effect |
| --- | --- | --- | --- | --- |
| `POST /phone/pair` | `{code, name?}` | 200 `{ok, phone:{id, name}}` and `Set-Cookie` | 202 `{pending:true}` while the PC has not answered. 400 bad code shape. 403 wrong/expired/refused/not a phone/cluster PC. 409 code taken. 429 after 5 misses / 10 min. 401 no tailnet address | Writes `phone.json` only after the PC approves |
| `POST /phone/pair/status` | `{code}` | same 200 when approved | 202 while waiting. 400 if the code was never claimed | Collects the cookie. Does not approve |
| `GET /phone/api/me` | cookie | 200 `{phone, host, main:{id, label}}` | 401 `{error, pair:true}` if unpaired. 403 login mismatch | May touch `last_seen` at most once a minute |
| `GET /phone/api/overview` | cookie | 200 `{devices, sessions, ask:{enabled}}` | 401 / 403 as above | Read. Devices include `network.lan_ip` and meters. This is not the fleet projection |
| `GET /phone/api/fleet` | cookie | 200 phone inventory | 401 / 403. 500 generic, path stripped | No subprocess, no probe |
| `POST /phone/api/session/start` | `{device, kind?:"screen", quality?:"low"}` | 200 hub view (`id`, …) | 400 unknown device/kind/quality. 409 busy / wrong kind. 403 permission. 503 hub down | Opens a screen or camera session as `phone:<id>`. Publishes `phone.session` and, for camera, `camera.on` |
| `GET /phone/api/session/frame?session&after` | query | 200 JPEG plus `X-Frame-Seq/Width/Height`, or 204 plus `X-Node-Ago` | 410 session ended. 429 one frame request at a time. 404 not yours (same text as missing) | Read |
| `POST /phone/api/session/input` | `{session, events}` ≤ 200 events | 200 `{ok, queued}` | 400 bad events. 409 camera (no input). 404 | Injects into that phone's session only |
| `POST /phone/api/session/stop` | `{session}` | 200 `{ok, session}` | 404 | Closes the session and releases keys |
| `POST /phone/api/ask` | `{text, device?, key?}` text 1–2000 | 200 `{reply, job, trace, usage}` or `{pending:true, job, trace}` or `{waiting:true, reply:"Waiting for your OK on the PC."}` | 400 empty, too long, or an approve/deny word. 409 still on the last request, or target has no remote. 502 `{error, job, trace}` when the job errors. 503 no console | Submits on the main. Confirmations stay on the PC. `key` (8–64 `[A-Za-z0-9_-]`) retries the same text instead of a second job |
| `GET /phone/api/ask/<job>` | `?after=<rev>` optional | 200 same answer shape. Holds up to 8 s | 404 if the id is not this phone's job | Read |
| `POST /phone/api/logout` | `{}` | 200 `{ok:true}` and a cleared cookie | 401 if already gone | Revokes that phone and ends its sessions |
| `POST /phone/api/fleet…` or `/api/fleet/…` | any | never | 404 (or 405 if the path is not POST-shaped) | None. **Verified this session** (`test_there_is_no_phone_write_route`) |

`kind` is `screen` or `camera`. `quality` is `low`, `medium`, or `high`. One phone cannot
see or stop another's session. Ask never approves a confirmation.

## Cluster `/v1` (node agent, not the page)

| Route | Who | Success | Refusal |
| --- | --- | --- | --- |
| `GET /v1/status` | any authenticated token | 200 `{main_device, code, nodes}` | 403 bad token or wrong `X-JARVIS-Main` |
| `GET /v1/code` | node, not owner | 200 signed bundle | owner token falls through and 403s |
| `GET /v1/jobs/<id>?wait=` | owner: any job. Node: only jobs it requested | 200 job. Holds up to `POLL_WAIT` (25 s) | 404 unknown |
| `POST /v1/redeem` | node | 200 `{token, key_rotated}` | repeating with the same proposed token does not write a new SSH key |
| `POST /v1/poll` | node | 200 `{job, main_device, code}`. Job is signed or null | kinds limited to the node's capabilities. `desktop` adds `remote`. `files` adds `storage` and `transfer` |
| `POST /v1/result` | node | 200 `{accepted:true}` | 400 / 404 drop the result for good. Other failures keep the outbox |
| `POST /v1/remote/frame` and `/v1/remote/input` | node with `desktop` or `camera` | 200 session result | 403 if neither capability, before any session lookup |
| `POST /v1/action` | owner token | 200 dispatch | 403 for `remote`, `camera`, `locate` |
| `POST /v1/control` | `console`, or `files` only when `kind=transfer` and `node` is the main | 200 dispatch | node cannot target the main except that transfer, and cannot run `desktop` `terminal` |
| `POST /v1/ask` | capability `ask` | **202** `{id}` | `write:true` needs the owner token. Text 1–20000. Tier `simple\|medium\|complex` or absent. Timeout 1–1800 |

Body cap: 64 KiB, or 4_000_000 for result, action, control, and remote frame.
`jarvis/cluster.py:33-34`, `:1060-1064`.

`/v1/poll` may include `meters` from the node. That sample is a host probe, not a
measured Dell battery. Do not present it as one.

## Console routes the page already calls

Groups, not a widget list. GETs are 200 unless noted. POSTs need `X-JARVIS: 1` and a
JSON object except the two streamed uploads. A node token is 403 on every row marked
owner.

| Group | Routes | Write? | Node token | Notes for a client |
| --- | --- | --- | --- | --- |
| Live | `GET /api/events` | no | 403 | SSE. `Last-Event-ID` or `?after=`. 503 when 16 streams are open. Hello event has no id |
| Read models | `GET /api/overview` `/api/credits?refresh=1` `/api/graph/system` `/api/graph/heap?case=` `/api/tree` `/api/specialists` `/api/route?text&tier` `/api/probe?refresh=1` `/api/usage?hours=` `/api/system/design` `/api/system/components` | no | overview, route, specialists, graph/system, usage, credits only | Heap, probe, sysview are owner |
| Memory / history | `GET /api/memory` `/api/memory/item?id=` `/api/history?limit=` `/api/history/commands?limit=` `/api/cases` `/api/export?case=` (markdown download) `POST /api/memory` `POST /api/memory/link` `POST /api/cases` | POST writes memory or a case | 403 | Export `Content-Disposition` filename is sanitized |
| Plans / tasks / jobs | `GET /api/plans/<id>` `/api/jobs/<id>` `/api/tasks/<id>` `POST /api/plans/<id>/run` `POST /api/vault/tasks` `POST /api/vault/tasks/<id>` `GET /api/vault/tasks?q=` `/api/vault/graph` `/api/vault/brief` | run and task POST write | own `GET /api/jobs/<id>` only | Plan run and task bodies are owner content |
| Ask / confirm | `GET /api/confirm` `POST /api/confirm` `POST /api/ask` `{text, tier?, speak?, case?}` | POST confirms or starts a turn | yes, with `ask`. Confirm pending is origin-scoped | Ask spends subscription quota. Do not add a phone path that approves |
| Voice | `GET /api/voice` `/api/voice/level` `/api/voice/setup` `/api/voice/devices` `POST /api/voice` `{action}` `POST /api/voice/setup` | POST changes the listener or enrollment | `GET /api/voice` only | Setup and devices stay on the PC that has the mic when proxied |
| Settings | `GET /api/settings` `POST /api/settings` `{changes, confirmed?}` | POST writes allow-listed fields | 403 | MAIN key is 400 and is not written. Loosening keys need `confirmed:true` or 400. Reply adds `saved` and `restart` |
| Fleet | the fleet table | yes, on the main | 403 every spelling. **Verified this session** | `HubAuthTests.test_node_tokens_are_denied_in_every_spelling` |
| Connections | `GET /api/connections` `POST /api/connections` `{target:"here", part:"voice", action:start\|stop\|restart}` | POST starts or stops the local voice daemon | 403 | Not fleet admin. Unknown part/action is 400 |
| Peripherals | `GET /api/peripherals` `POST /api/peripherals` | POST acts on this PC | 403 on the hub; local on a node proxy | Answered on the machine you are sitting at |
| Remote | `GET /api/remote` `/api/remote/frame?session&after` `POST /api/remote/start` `/input` `/stop` | POST drives a node | 403 | Frame 200 JPEG, 204 with `X-Node-Ago`, or 410 `{error}` when the session ended |
| Screen / files | `GET /api/screen?device=` `/api/fs?device&path` `/api/fs/devices` `/api/fs/thumbnail` (PNG) `/api/inbox?device=` `POST /api/fs/copy` `POST /api/send?name&device` (raw body) | copy and send write files | 403 | `/api/send` is streamed. A mid-body failure closes the connection |
| Knowledge | `GET /api/kb/state` `/api/kb/graph` `/api/kb/page?slug=` `POST /api/kb/upload?name=` `POST /api/kb/resolve` `POST /api/kb/command` | POST writes the inbox or a resolution | 403 | Upload is streamed, 60 s socket timeout |
| Agent / Grok | `GET /api/agent` `/api/agent/voices` `/api/agent-browser` `/api/grokbot` `POST /api/agent/session` `/api/agent/voice` `/api/agent/voice/preview` `/api/agent/ask` `/api/agent-browser` `/api/grokbot` | POST starts sessions and commands | status GETs for agent and grokbot only. POST 403, including grokbot `terminal` | Owner |
| Credits | `POST /api/credits/manual` `{provider, remaining, total, unit?, note?}` | writes a manual balance | 403 | |
| Overlay | `POST /api/overlay` `{action?}` | asks the overlay window | local on a node; 404 if this process is not the overlay | |
| Static | `GET /` and files under `jarvis/web` | no | served by whichever process is bound | ETag revalidation, `Cache-Control: no-cache`. Unknown path 404 `{error:"Not found."}` |

`POST /api/listen` is raw audio, not JSON, with `?verify=` and `?wake=`. Node with `ask`
may post it. It is a transcription call, not a fleet call.

## Known broken, unimplemented, and stale audit rows

Do not file the missing lease as a regression. Do not treat a green parent suite as
coverage of the rows marked open.

| Item | Severity | Where | What a client should do | State |
| --- | --- | --- | --- | --- |
| Settings MAIN rewrite | was high | `settings.py:261`, `settings.js:193` | Never POST `cluster.main_device`. Show the blocked sentence | **Blocked.** Verified by the Settings test this session. Supersedes `AUDIT-CONSOLIDATION-SURFACES.md` finding 1 |
| Stale device write clobbering a concurrent `main_device` | was high | `config_lock.py:152-156`, `fleet_management.py:524-546` | On 409 `config_changed`, reload and offer a retry. Do not assume the invite landed | **Refused in this tree.** **Verified this session:** `test_role_flip_between_device_read_and_replace_is_not_clobbered` keeps `dell` and the marker, writes no node row. `AUDIT-CONSOLIDATION-BACKEND.md` finding 1 is stale |
| Role flip during signing-key generation still issuing a credential | was high | `cluster_setup.py:484-490` | Same: a refusal means nothing was enrolled | **Refused before authorize.** **Verified this session:** `test_role_flip_while_the_signing_key_is_made_stops_before_any_credential`. Finding 2 in that audit is stale. Still not a lease |
| Compare-then-rename window for writers that skip the fleet lock (`settings.write`, a hand editor) | medium | `config_lock.py:15-18` | Retry `config_changed`. Do not offer Settings as a second MAIN control | **Open.** Not executed as two racing threads here. Inferred from the lock comment |
| CLI `jarvis cluster main` and a hand edit of `cluster.main_device` | high if used as handoff | `config_lock.py:3-6` | No button, no shell-out | **Still possible.** Not safe handoff. Not the Settings HTTP path |
| Invite `uncertain` / `retryable:false` / `recover:"revoke"` | medium | `fleet_management.py:715-728`, `:799-805` | Tell the user to revoke that node. Do not mint another code for the same id | **Open contract.** Receipt file is the ledger. Parent suite does not replace the dedicated tests |
| Oversize held task head blocks later revisions; archive `held` stays | high for archive status, not for fleet JSON shape | `AUDIT-CONSOLIDATION-BACKEND.md` finding 3 | Show `archive_held` as held. Do not relabel it current | **Open.** Not re-run here |
| Oversized HTTP body can reset before 413 | medium | `ui.py:1415-1427`, `AUDIT-SUMMARY.md` | Treat a dropped upload as failure | **Open.** Not re-run here |
| Safe MAIN transfer, phone fleet-admin, cloud provisioning, C3 canonical read, gateway, distributed compute | n/a | fleet `missing[]`, `IMPLEMENTATION-PROGRESS.md` | Render `blocked` / `unsupported`. Do not enable | **Not built.** By design |
| Phone `fleet()` docstring says invite state is omitted | low | `phone.py:1033-1035` | Render `status` and `step`. The docstring is the bug | **Doc drift.** Test expects `invite_pending` |
| `FLEET-API.md` says configure/hand-edit skip the lock, and a role change after the second read is unseen | low | `FLEET-API.md` lines 119-122 | Follow this file | **Doc drift.** Code and `config_lock.py` differ |

`AUDIT-CONSOLIDATION-BACKEND.md` finding 4 (rollback restores a token and leaves jobs
`uncertain`) does not match `invite()` now: `enroll(..., settle_jobs=False)` and
`settle_rotated` run only after the code exists (`cluster_setup.py:509-526`). Not re-run
this session. Do not rebuild UI copy around the old "jobs left uncertain on a retryable
failure" sentence unless a newer test shows it.

## Evidence this session

Command (temp dirs, `.venv`, no live service):

```sh
.venv/bin/python -m unittest \
  tests.runtime.test_settings.SavedClusterTests.test_a_saved_main_shows_before_the_restart_and_no_broker_is_built \
  tests.cluster.test_fleet_management.ConsoleHTTPTests.test_transfer_routes_are_preview_or_409 \
  tests.cluster.test_fleet_management.ConsoleHTTPTests.test_post_gates \
  tests.cluster.test_fleet_management.ConsoleHTTPTests.test_main_and_error_codes_over_http \
  tests.cluster.test_fleet_management.HubAuthTests.test_node_tokens_are_denied_in_every_spelling \
  tests.cluster.test_fleet_management.HubAuthTests.test_owner_token_reaches_the_routes \
  tests.cluster.test_config_writers.ConsoleInviteLedgerTests.test_role_flip_between_device_read_and_replace_is_not_clobbered \
  tests.cluster.test_config_writers.InviteUnderLockTests.test_role_flip_while_the_signing_key_is_made_stops_before_any_credential \
  tests.cluster.test_node_console_privacy.NodeAuthorizationTests \
  tests.mobile.test_phone_fleet -v
```

Result: 20 tests, 2.795 s, OK.

Not done: full suite, browser pass, live `:7777` / `:7787`, real invite, real revoke,
Dell battery, vault read, node enrollment. Static claims about `settings.js` are from
reading the file. The parent 3434 count is from `IMPLEMENTATION-PROGRESS.md`, not from
this session.
