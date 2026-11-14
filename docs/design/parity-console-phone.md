# Console and phone parity

*Auth, error envelopes, route shapes, phone contract, and the visual tokens a new screen must keep. Not a production-ready claim and not a screen catalog.*

Fleet bodies are [fleet-api.md](fleet-api.md). Fleet layout is [docs/handbook/fleet-ui.md](../handbook/fleet-ui.md). Phone operator notes are [docs/handbook/phone.md](../handbook/phone.md). Drag-and-drop is [docs/handbook/drag-drop-design.md](../handbook/drag-drop-design.md).

Keep every route below. Do not remove a write, change a status code, or invent a gateway, a lease, a working handoff button, or a "vault is primary" claim. Future MAIN lease, distributed compute, and C3 vault-primary are not this surface. Obsidian C2 is the live writer when capture is on. SQLite stays the personal source. See [obsidian-storage.md](obsidian-storage.md).

## Auth

Four callers. They do not share a credential.

| Caller | Proof | May call | Must not |
| --- | --- | --- | --- |
| Owner console | Loopback `Host` allow-list. `Sec-Fetch-Site` `same-origin` or `none`, or the header absent. POST also `X-JARVIS: 1`. No bearer | Every `/api/*` on that process, including fleet writes when this PC is the enabled main | Cross-site, wrong Host, or POST without `X-JARVIS` → 403 before the body is acted on |
| Node console proxy | Same loopback gates, then the node's bearer with `X-JARVIS-Main` equal to the hub main | `NODE_API_READ` GETs. POST `/api/ask`, `/api/listen`, `/api/confirm` only with capability `ask`. Own job `GET /api/jobs/<id>` | Settings, fleet, files, screen, memory, history, events, plans, uploads, Grokbot commands. 403 |
| Node agent on the hub | `Authorization: Bearer <node token>` and `X-JARVIS-Main` equal to `hub.main` | `/v1/poll`, `/v1/result`, `/v1/redeem`, `/v1/code`, `/v1/remote/frame`, `/v1/remote/input` when the capability matches. `/v1/control` with `files` only for `kind=transfer` aimed at the main | `/v1/action` (owner only). Camera, locate, and remote-session start over HTTP. Write-runs (`/v1/ask` with `write:true`) |
| Paired phone | Port 7787 only. Host is the tailnet name or loopback. `X-Forwarded-For` is a tailnet address `whois` knows. `Tailscale-User-Login` matches. Cookie `__Host-jarvis_phone`. POST also `X-JARVIS-Phone: 1` and JSON ≤ 256 KiB | The phone table | Any console path (404, no fall-through). Any fleet write (404). Funnel or an unreadable serve config → 503 for every request |

The owner hub token (`hmac` against the hub owner secret) reaches the full console API. It still cannot start `remote`, `camera`, or `locate` over `/v1/action` or `/v1/control`.

Node field cutting: `GET /api/overview` keeps `version`, `now`, `cluster`, `devices`, `routing`, `models`, `brain`, `live`, `voice`, `tools`. `GET /api/confirm` keeps `pending`, `grants`. Other allowed reads are not field-cut.

Local-only on a node proxy, never forwarded: `GET/POST /api/voice/setup`, `GET /api/voice/devices`, `GET/POST /api/peripherals`, `POST /api/overlay`.

## Errors

| Source | Status | Body |
| --- | --- | --- |
| `FleetError` | the exception's status | `{error, code, retryable, step?, …}`. `Cache-Control: no-store`. Switch on `code` |
| Other console `ValueError` (settings, bad JSON) | 400 | `{error}` only. No `code` |
| Console `LookupError` | 404 | `{error}` |
| Console anything else | 500 | Generic text. `OSError` omits the path. `RuntimeError` text is passed through |
| Phone `Reply` | the status it carries | `{error, …extras}`. No fleet `code` |
| Hub `PermissionError` | 403 | `{error}` |
| Hub `NodeOffline` | 409 | `{error, code:"node_offline"}` |
| Hub `ValueError`, `TypeError`, `OSError`, `RuntimeError` | 400 | `{error}` |

Oversized console JSON or audio (`Content-Length` above `MAX_AUDIO`, 45 seconds of float32) is 413 `{error:"Too large."}`. A declared length past the drain cap can reset the socket instead of delivering 413. Do not treat a reset as success.

Upload drain bounds: `REJECT_DRAIN_BYTES = MAX_AUDIO + 1 MiB`, `REFUSE_DRAIN_BYTES` 64 KiB, `REJECT_IDLE_S` 2, `REJECT_DEADLINE_S` 8, `DRAINS` a `BoundedSemaphore(8)`, read block 64 KiB. A declared length far beyond the cap is rejected from the header with no allocation. The reset is the accepted trade-off. A bad, conflicting, or chunked `Content-Length` is 400 `Bad Content-Length.` Lengths over 18 digits clamp to `10**18`.

## Settings and main

`GET /api/settings` still returns `cluster.main_device` as a choice of device ids. `POST /api/settings` `{changes, confirmed?}` writes allow-listed fields. Loosening keys (`LOOSENS`) need `confirmed:true` or the save is 400 and nothing is written. The reply adds `saved` and `restart`.

`cluster.main_device` is accepted only as the sole change, and only through `set_main`. Combining it with other settings raises before any save. Success is the handoff outcome, not a raw file edit. See [fleet-api.md](fleet-api.md). The settings page keeps the field hidden and does not offer a Make-main button. A fleet preview of `support:"blocked"` is not a button that moves the role.

## Console routes

GETs are 200 unless noted. POSTs need `X-JARVIS: 1` and a JSON object except the streamed uploads. A node token is 403 on every owner row.

| Group | Routes | Node token |
| --- | --- | --- |
| Live | `GET /api/events` SSE. `Last-Event-ID` or `?after=`. 503 at 16 streams. Hello has no id | 403 |
| Read models | `GET /api/overview`, `/api/credits?refresh=1`, `/api/graph/system`, `/api/graph/heap?case=`, `/api/tree`, `/api/specialists`, `/api/route?text&tier`, `/api/probe?refresh=1`, `/api/usage?hours=`, `/api/system/design`, `/api/system/components` | overview, route, specialists, graph/system, usage, credits only. Heap, probe, and sysview are owner |
| Memory / history | `GET /api/memory`, `/api/memory/item?id=`, `/api/history?limit=`, `/api/history/commands?limit=`, `/api/cases`, `/api/export?case=` (markdown). `POST /api/memory`, `/api/memory/link`, `/api/cases` | 403 |
| Plans / jobs / tasks | `GET /api/plans/<id>`, `/api/jobs/<id>`, `/api/tasks/<id>`, `/api/vault/tasks?q=`, `/api/vault/graph`, `/api/vault/brief`. `POST /api/plans/<id>/run`, `/api/vault/tasks`, `/api/vault/tasks/<id>` | own `GET /api/jobs/<id>` only |
| Ask / confirm | `GET /api/confirm`. `POST /api/confirm`. `POST /api/ask` `{text, tier?, speak?, case?}` | yes, with `ask`. Confirm pending is origin-scoped. Ask spends quota. Do not add a phone path that approves |
| Voice | `GET /api/voice`, `/api/voice/level`, `/api/voice/setup`, `/api/voice/devices`. `POST /api/voice` `{action}`, `/api/voice/setup` | `GET /api/voice` only. Setup and devices stay on the PC with the mic |
| Settings | `GET/POST /api/settings` | 403 |
| Fleet | the fleet table in [fleet-api.md](fleet-api.md) | 403 every spelling |
| Connections | `GET /api/connections`. `POST /api/connections` `{target:"here", part:"voice", action:start\|stop\|restart}` | 403. Not fleet admin |
| Peripherals | `GET/POST /api/peripherals` | 403 on the hub. Answered on the machine you are sitting at when proxied |
| Remote | `GET /api/remote`, `/api/remote/frame?session&after`. `POST /api/remote/start`, `/input`, `/stop` | 403. Frame is 200 JPEG, 204 with `X-Node-Ago`, or 410 `{error}` |
| Screen / files | `GET /api/screen?device=`, `/api/fs?device&path`, `/api/fs/devices`, `/api/fs/thumbnail`, `/api/inbox?device=`. `POST /api/fs/copy`, `/api/send?name&device` (raw body) | 403. `/api/send` is streamed |
| Knowledge | `GET /api/kb/state`, `/api/kb/graph`, `/api/kb/page?slug=`. `POST /api/kb/upload?name=`, `/api/kb/resolve`, `/api/kb/command` | 403. Upload is streamed, 60 second socket timeout |
| Agent / Grok | `GET /api/agent`, `/api/agent/voices`, `/api/agent-browser`, `/api/grokbot`. POSTs under `/api/agent/session`, `/api/agent/voice`, `/api/agent/voice/preview`, `/api/agent/ask`, `/api/agent-browser`, `/api/grokbot` | status GETs for agent and grokbot only. POST 403, including grokbot `terminal` |
| Credits | `POST /api/credits/manual` `{provider, remaining, total, unit?, note?}` | 403 |
| Overlay | `POST /api/overlay` `{action?}` | local on a node. 404 if this process is not the overlay |
| Static | `GET /` and files under `jarvis/web` | ETag, `Cache-Control: no-cache`. Unknown path 404 `{error:"Not found."}` |

`POST /api/listen` is raw audio, not JSON, with `?verify=` and `?wake=`. A node with `ask` may post it. It is a transcription call, not a fleet call.

## Cluster `/v1`

Body cap 64 KiB, or 4_000_000 for result, action, control, and remote frame.

| Route | Who | Success | Refusal |
| --- | --- | --- | --- |
| `GET /v1/status` | any authenticated token | 200 `{main_device, code, nodes}` | 403 bad token or wrong `X-JARVIS-Main` |
| `GET /v1/code` | node, not owner | 200 signed bundle | owner token falls through and 403s |
| `GET /v1/jobs/<id>?wait=` | owner: any job. Node: only jobs it requested | 200. Holds up to `POLL_WAIT` (25 s) | 404 |
| `POST /v1/redeem` | node | 200 `{token, key_rotated}` | repeating the same proposed token does not write a new SSH key |
| `POST /v1/poll` | node | 200 `{job, main_device, code}`. Job is signed or null | kinds limited to the node's capabilities. `desktop` adds `remote`. `files` adds `storage` and `transfer` |
| `POST /v1/result` | node | 200 `{accepted:true}` | 400 or 404 drops the result. Other failures keep the outbox |
| `POST /v1/remote/frame` and `/input` | node with `desktop` or `camera` | 200 | 403 if neither capability, before session lookup |
| `POST /v1/action` | owner token | 200 dispatch | 403 for `remote`, `camera`, `locate` |
| `POST /v1/control` | `console`, or `files` only when `kind=transfer` and `node` is the main | 200 | a node cannot target the main except that transfer, and cannot run desktop `terminal` |
| `POST /v1/ask` | capability `ask` | 202 `{id}` | `write:true` needs the owner token. Text 1–20000. Tier `simple`, `medium`, `complex`, or absent. Timeout 1–1800 |

`/v1/poll` meters are a host probe, not a measured Dell battery.

## Phone routes

Shell files are the fixed `SHELL` map. Everything else is 404. Methods other than GET and POST are 405. Pairing alphabet is 8 characters. Spaces and dashes are ignored. The listener is `127.0.0.1:7787` unless `phone.port` is in 1024–65535.

| Route | Success | Failure | Effect |
| --- | --- | --- | --- |
| `POST /phone/pair` `{code, name?}` | 200 `{ok, phone:{id, name}}` and `Set-Cookie` | 202 `{pending:true}` while the PC has not answered. 400 bad shape. 403 wrong, expired, refused, not a phone, or a cluster PC. 409 code taken. 429 after 5 misses / 10 min. 401 no tailnet address | Writes `phone.json` only after the PC approves |
| `POST /phone/pair/status` `{code}` | same 200 when approved | 202 while waiting. 400 if the code was never claimed | Collects the cookie. Does not approve |
| `GET /phone/api/me` | 200 `{phone, host, main:{id, label}}` | 401 `{error, pair:true}` if unpaired. 403 login mismatch | May touch `last_seen` at most once a minute |
| `GET /phone/api/overview` | 200 `{devices, sessions, ask:{enabled}}` | 401 / 403 | Read. Devices include `network.lan_ip` and meters. Not the fleet projection |
| `GET /phone/api/fleet` | phone inventory | 401 / 403. 500 generic, path stripped | No subprocess, no probe. See [fleet-api.md](fleet-api.md) |
| `POST /phone/api/session/start` `{device, kind?:"screen", quality?:"low"}` | 200 hub view | 400 unknown device, kind, or quality. 409 busy or wrong kind. 403. 503 hub down | Opens a screen or camera session as `phone:<id>`. Publishes `phone.session` and, for camera, `camera.on` |
| `GET /phone/api/session/frame?session&after` | 200 JPEG plus `X-Frame-Seq/Width/Height`, or 204 plus `X-Node-Ago` | 410 ended. 429 one frame request at a time. 404 not yours, same text as missing | Read |
| `POST /phone/api/session/input` `{session, events}` ≤ 200 events | 200 `{ok, queued}` | 400 bad events. 409 camera. 404 | Injects into that phone's session only |
| `POST /phone/api/session/stop` `{session}` | 200 `{ok, session}` | 404 | Closes the session and releases keys |
| `POST /phone/api/ask` `{text, device?, key?}` text 1–2000 | 200 `{reply, job, trace, usage}` or `{pending:true, job, trace}` or `{waiting:true, reply:"Waiting for your OK on the PC."}` | 400 empty, too long, or an approve/deny word. 409 still on the last request, or the target has no remote. 502 `{error, job, trace}` when the job errors. 503 no console | Submits on the main. Confirmations stay on the PC. `key` (8–64 `[A-Za-z0-9_-]`) retries the same text |
| `GET /phone/api/ask/<job>?after=` | same answer shape. Holds up to 8 s | 404 if the id is not this phone's job | Read |
| `POST /phone/api/logout` `{}` | 200 `{ok:true}` and a cleared cookie | 401 if already gone | Revokes that phone and ends its sessions |
| `POST /phone/api/fleet…` or `/api/fleet/…` | never | 404, or 405 if the path is not POST-shaped | None |

`kind` is `screen` or `camera`. `quality` is `low`, `medium`, or `high`. One phone cannot see or stop another's session. Ask never approves a confirmation. Camera input is 409.

Hash routes: `#boot`, `#down`, `#pair`, `#/devices`, `#/device/:id`, `#/control/:id`, `#/camera/:id`, `#/ask`, `#/phone`, `#/fleet`. Three tabs only: Devices, Ask, Settings. Control is the primary device action, above camera. Sessions do not start on load, reload, or Back. Only a tap that sets `want` starts one. No location, radar, or geolocation. Forget is two taps within 4 seconds. Quality default is `low`, stored at `jarvis.phone.quality`. Hidden for 15 seconds (`HIDDEN_STOP_MS`) ends a screen session. Disconnect and hide still release held keys.

## Liveness and archive words

`connections.presence(seen, busy)`: online means a check-in within 75 seconds (proof). Busy means past 75 seconds but a running job still inside its deadline plus 15 seconds, labeled "busy, awaiting contact". Offline is neither. Online stays proof-only. `link_state()` is unchanged on purpose and remains the invite and enrolled guard. Phone online is the broker check-in window. `hub.alive` is the fallback only when there is no broker row.

Capability chips show titles. The id stays in the payload. Ack copy uses titles, singular or plural, an 80-character cap, and `textContent`.

A Linux SSH probe with `bats=missing` must not replace a valid CIM battery on a Windows node card.

Archive UI words: `files_stale`, `archive_baseline_pending`, `archive_baseline_seeded`. "Partly verified" is its own state. Never claim "capture complete". Git "null ahead" reads "unknown".

Invite secrets stay out of the DOM text dump. A non-retryable `invite_failed` shows the server reason. The page remembers the uncertain request in memory only. Reload forgets it. The server still refuses the same id.

## Navigation the console must keep

Hashes: `#console`, `#credits`, `#memory`, `#files`, `#devices`, `#remote`, `#settings`, `#run`, `#graph`, `#system`, plus `knowledge.html` and `design.html`. Do not drop `#view-graph` even though `#graph` redirects to `knowledge.html`.

Rail keys 1–9: console, knowledge, system, credits, memory, files, connections, remote, settings. Key 3 and `#system` open `#view-system`, which is the other screen. `#run` has no rail key. There is no task board. The agenda API may exist. The console camera is not a console screen. Vault general editing stays off.

One non-plan ask job at a time. The UI waits 15 minutes, then links `#run`. Esc does not cancel the server job. Always-on listen sends `?wake=1` for a waiting clip. Space push-to-talk omits it. Driving Space as `wake=1` in a test does not match the DOM.

Breakpoints: console rail at ≥1181 px is a labeled 196 px column, 641–1180 is an icon rail at 64 px, ≤640 is a bottom bar. Cockpit at ≤1180 puts the pipeline beside the assistant, and at ≤760 it is one column. Phone at ≤359 uses 16 px gutters and a 29 px title. Landscape ≤520 px tall turns the tabs into a left rail. `data-kb=1` hides the tab bar. The Remote header is compact at ≤640 px so the expanded header does not cover the image.

## Visual tokens

`console.css` does not load last. Order in `index.html`: `app.css`, `porcelain.css`, `runview.css`, `flow.css`, `sharedinput.css`, `peripherals.css`, `console.css`, then lab, cockpit, settings, files, connections, fleet, laptops, remote, voicesetup. `phone.css` is the only phone sheet. From its later redesign section, repeated properties win. `checkpoint.css` is not part of this contract.

Theme key `jarvis.theme` is `system`, `light`, or `dark`. Default is dark.

| Token | Dark | Light |
| --- | --- | --- |
| `--bg` | `#000` | `#fff` |
| `--ink` | `#ededed` | `#0a0a0a` |
| `--ink-2` | `#a8a8a8` | `#4a4a4a` |
| `--ink-3` | `#8a8a8a` | `#6b6b6b` |
| `--needs` | `#f0a640` | `#9a5600` (waiting only) |
| `--bad` | `#ef7a6c` | `#b3261e` (failed or destructive only) |

Light theme on `#e7e7e9`: `--ok #2a6e3e`, `--warn #865700`, `--alert #b1342b`. The older `#2f7a45` / `#9a6400` / `#c0392f` fail WCAG AA. Dark theme status tokens stay as they are. Colour is state only. Dark `--ok #e8e8e8` against console ink `#ededed` is about 1.05:1. AA on black holds. Distinctness of those two grays does not. `--jv-faint #555` on `#000` (2.82) and `#a0a0a0` on `#fff` (2.61) fail AA for small text. Small text uses `--jv-ink-3`.

Type: Geist, Geist Mono, and Doto for the wordmark only. Console body 14 px / 1.5. Phone body 16 px, and fields at least 16 px so iOS does not zoom. Phone title 32 px / 600. Console title 22 px / 500. Radius: console 2 px, phone controls 12 px, phone device rows, ask bubbles, and the decision tree 16 px. Motion 180 ms `cubic-bezier(.16,1,.3,1)`. Reduced motion stops it.

## QA bar

A change to these surfaces is accepted only when:

- The four callers still cannot use each other's credentials, and a node still cannot read history, events, tasks, memory, settings, or files.
- Fleet failures still branch on `code`. Settings refusals are still `{error}` with no `code`.
- Phone POST fleet and phone console fall-through stay 404. Funnel or an unreadable serve config is 503.
- Ask never approves. Camera input stays 409. Sessions do not start on load.
- Preview of main transfer stays read-only. POST runs `set_main` only with an explicit config file and a bound handoff, and otherwise stays 409 `blocked` without guessing a path.
- Online stays a 75-second proof. Busy is the deadline-plus-15-second state. `link_state` is not rewritten to match presence.
- Archive copy does not say capture is complete. A gap or a damaged head is not `current`.
- New UI does not add a gateway, a lease, a handoff button that skips the coordinator, or a claim that the vault is primary.
- Light-theme `--ok`, `--warn`, and `--alert` stay the AA pair above. Small text does not use `--jv-faint`.
- Invite codes and receipts never appear in a DOM text dump or a log line.
