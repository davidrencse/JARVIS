# Console feature catalog (pre-implementation parity)

Date: 2026-11-11. Read-only. One of five non-overlapping auditors. Scope is the owner console only: screens, navigation, actions, and the `jarvis/ui.py` routes those screens call. Phone screens and backend races are named only where the console must not be confused with them.

This is a preservation list for a later UI. It is not a production-ready claim. Parent suite 3434 passed before the last Settings patch, with open consolidation defects (`AUDIT-SUMMARY.md`). This pass did not rerun that suite, did not start a server, and did not open the live vault, private databases, or tokens.

## How this was checked

| Item | Result |
|---|---|
| Branch | `implementation/ui-parity-checkpoint-20261111` |
| HEAD | `845b55800a2243e826fa5096bbb8520c1ac8dd86` |
| Working tree | `git status --porcelain` counted **231** dirty paths. Catalog is the working tree, not HEAD alone. |
| Docs read | `CLAUDE.md`; `IMPLEMENTATION-PROGRESS.md`; `AUDIT-SUMMARY.md`; `AUDIT-CONSOLIDATION-SURFACES.md`; `FINAL-SURFACE-REPAIRS.md` (console rows only). |
| Code read | `jarvis/web/index.html`, `knowledge.html`, `design.html`, the console scripts they load, `jarvis/ui.py` route table, `jarvis/settings.py` `FIELDS`. |
| Tests run | None. Named tests below are files that already pin the behavior. **Source** means the current file says it and this pass did not execute it. |
| Browser | Not opened. No live invite, remote, camera, or microphone. |

Static files are served by `ui.py` `_static` (`ui.py:1658-1674`) from `jarvis/web/`. `/` is `index.html`. Posts require `X-JARVIS: 1` and a same-origin host (`tests/web/test_ui_security.py`).

## Preserve these, as they actually are

| Capability | Where it lives today | Do not |
|---|---|---|
| Ask | Dock on every `index.html` view. `POST /api/ask`. One non-plan job at a time (`ui.py:877-878`). | Remove the dock, tiers, or the 15-minute wait. |
| Voice | Dock mic + ear; Settings Voice and Calls; Connections “Voice listener”; `voicesetup.js`. | Collapse listener, push-to-talk, and the ElevenLabs call into one control. |
| Approvals | Gate on the console, and on other views while something waits. Flow replay can also confirm. | Drop Allow / 10-minute grant / Decline / revoke. |
| Files | `#files` two-laptop browser, plus Connections “Send files” and “Received files”. | Treat SSH copy, drop-folder send, and thumbnails as one action. |
| Tasks | **No task board.** Agenda API exists. Agent plans open `#run`. Ask still routes “add a task” through tools. | Build a board and call it the old UI, or delete the API. |
| Vault | Settings toggles, Fleet “Memory archive and Git” (read-only), Knowledge vault layer. General editing stays off. | Paint C2 status as a canonical C3 store, or add a note editor. |
| Remote | `#remote` screen stream plus keyboard and mouse. Main console only. | Mix it with the view-only Screens snapshot or with the phone camera. |
| Camera | **Not a console screen.** Fleet can grant the `camera` capability. The phone has View camera. | Add a webcam view to the desktop shell as if it were already there. |
| Fleet | Connections page, `#fleet-root`: inventory, invite, revoke, permissions, archive, blocked MAIN move. | Re-enable a Settings “Make main” write. |
| Stats | Console header counts, Usage tab, Credits, host meters, Remote fps/resolution/age, Knowledge counts. | Merge host meters and the hardware spec line into one RAM number. |

## Navigation

Rail order is `index.html:42-50`. Number keys follow `app.js:119-125`, not the rail’s page targets.

| Key | Rail label | Opens | In-page view | Expected |
|---|---|---|---|---|
| 1 | Console | `#console` | `#view-console` | Cockpit. Default when the hash is empty or unknown. |
| 2 | Knowledge | `knowledge.html` | none (separate page) | `#graph` and key 2 `location.replace("knowledge.html")` (`app.js:123-124`). |
| 3 | System | `design.html` | none on that click | Key 3 and `#system` open the **other** screen, `#view-system` (`system.js`). See residual 1. |
| 4 | Credits | `#credits` | `#view-credits` | Provider table. |
| 5 | Memory | `#memory` | `#view-memory` | Search and capture. |
| — | — | `#run` | `#view-run` | No rail item and no number key. Trace links here when a plan exists (`app.js:1110`, `1119`). |
| — | — | `#graph` | `#view-graph` | **Unreachable.** Redirect fires before the view is shown. The canvas, layers, and inspector remain in the HTML. Mini-graph on the console is the live memory drawing. |
| 6 | Files | `#files` | `#view-files` | `files.js` mounts `#files-wrap`. |
| 7 | Connections | `#devices` | `#view-devices` | Computers, fleet, Connect & share, screens, send. |
| 8 | Remote | `#remote` | `#view-remote` | `remote.js` mounts `#rm-root`. |
| 9 | Settings | `#settings` | `#view-settings` | Title has no “(9)”; the key still lands here. |

`#view-graph` code (`loadGraph`, `app.js:480`) only runs if `show()` selects `graph`, which the `#graph` redirect prevents. **Source.** Preserve `knowledge.html` as the graph the rail and key 2 open. Preserve the console mini-graph. Do not delete `#view-graph` in a parity pass without an explicit removal; it is legacy, not a second live screen.

Shell chrome on `index.html` (not on the two satellite pages, which have their own rail back to `index.html#console`):

| Control | Action | API | Expected | Evidence |
|---|---|---|---|---|
| Brand | `#console` | — | Home. | `index.html:36` |
| Case select | Pick or create an investigation. Hidden on the console view (`app.js:127`). | `GET/POST /api/cases` | New notes and asks are filed into it. Stored in `jarvis.case`. | `tests/web/test_ui.py` `test_case_capture_link_search_export` |
| Export | Download Markdown. Shown only when a case is selected. | `GET /api/export?case=` | Browser download, not a POST. | same test |
| Clock | Local date, time, zone. | — | Header chips. No API. | `index.html:63-77` |
| Search memory | Submits to Memory with the query. | then `GET /api/memory` | `dashboard.js:68-71` sets `#memory`. | source |
| Live chip | Connection to the event stream. | `GET /api/events` | “Live” or “Reconnecting”. Stream covers console, voice, and CLI. | `app.js:1467-1476` |
| Replay | Open the flow history panel. | `GET /api/history/commands?limit=12` | `flow.js` + `#flow-history`. | source |
| Recheck | Clicks probe refresh and credits refresh. | `GET /api/probe?refresh=1`, `GET /api/credits?refresh=1` | `dashboard.js:74` | source |
| Theme | system → light → dark. | — | `localStorage jarvis.theme`. | `app.js:142-149` |
| `/` or Ctrl+K | Focus the Ask box. | — | Ignored while typing in a control. | `app.js:1251` |
| Esc | Cancel mic, stop waiting, close graph inspector, or hide overlay. | `POST /api/overlay` only in overlay mode | Does not cancel the server job; the reply can still arrive. | `app.js:1109`, `1241-1245` |

Satellite rails (`knowledge.html:21-30`, `design.html:20-29`) repeat the same nine destinations.

## Ask, thread, and live pipeline

Screen: `#console` assistant column, plus the dock, which stays mounted on every hash view.

| Action | API | Expected | Evidence |
|---|---|---|---|
| Type and Enter, or Send | `POST /api/ask` `{text, tier, speak, case}` | Job id returns immediately. UI waits on `turn.done` / `turn.error`, polling `GET /api/jobs/<id>` if the stream drops. 15 min → trace link `#run`. | `app.js:1082-1136`; `tests/web/test_ui.py` `test_ask_runs_in_background` |
| Send while busy | none | Button becomes Stop waiting. Toast if a second ask is typed. Server refuses a second non-plan job: “JARVIS is still on the last request.” | `ui.py:877-878` source |
| Tier Auto / Quick / Tools / Plan | sent as `tier` null, `simple`, `medium`, `complex` | Segment on wide layout; `<select id="tier-select">` on narrow. Preview uses `GET /api/route`. | `app.js:1022-1034`, `1490` |
| Read replies aloud | `speak` on the ask body | This browser only (`jarvis.speak`). Kokoro or ElevenLabs depends on `GET /api/agent`. | `app.js:1035-1037`, `1358-1360` |
| Clear | none | Drops unsent and failed local lines. Memory history stays. | `app.js:998` |
| Thread | `GET /api/history?limit=40` | Console, voice, and CLI. Channels labeled. Usage chip when the reply has one. | `test_history_pairs_requests_with_replies` |
| Pipeline wheel + trace | `jarvis:live` from `/api/events` | Idle copy: waiting for “Jarvis, …”. Stages appear as the turn runs. | `wheel.js` source |
| Header stats | `GET /api/overview` | Runs today (or task-state total), memory items, machines online, voice listening/off, brain name and warm. | `wheel.js:418-429` |
| Agent screen | `GET /api/agent-browser`; `POST` `{action}` | Shown only while the agent browser is open. Buttons: Back, Reload, Stop scrolling, Close, Expand/Shrink. API also accepts `forward`; **no button**. Poll 700 ms, 400 ms while expanded, 4 s while closed. | `agentscreen.js`; `index.html:228-232`; `ui.py:1106-1110`; `test_console_buttons_are_a_closed_list` |
| Listener log | events | `<details>` under the thread. | `index.html:245` |

Cockpit tabs (`index.html:117-120`, `cockpit.js` only counts the session funnel):

| Tab | What it shows | API | Evidence |
|---|---|---|---|
| Machines | Link status, machine cards, host meters, Zenbook ping canvas. Check now. | `/api/overview`, `/api/probe` | `app.js:420-469`, `signals.js` |
| Usage | Codex and Claude 5-hour rings, other providers, DeepSeek Check. | `/api/credits`, `/api/usage?hours=24`, `/api/credits?refresh=1` | `signals.js:207` |
| Memory | Recent runs, mini force graph, heap facts, router preview. | `/api/history?limit=60`, `/api/graph/heap`, overview | `dashboard.js`, `app.js:471-478` |
| Activity | Agent state canvas and the session book. | overview + live events | `signals.js` source |

Host meters print backend blocks. They do not invent a charge percent. A Linux SSH probe with `bats=missing` must not replace a Windows CIM battery (`app.js:440-449`). Dell battery was not measured. Cluster-on is the deployed shape (probe does not SSH). **Source**, plus `tests/web/test_meters_web.py` and `tests/web/web_app_meters_card_test.cjs` named in `FINAL-SURFACE-REPAIRS.md`. Not re-run here.

## Voice

| Action | Where | API | Expected | Evidence |
|---|---|---|---|---|
| Click mic | Dock | later `POST /api/listen` | Toggles always-on. Default is on (`app.js:1011-1012`). Wake word “Jarvis” required. Own voice required. | `test_always_on_drops_anything_not_addressed_to_jarvis`, `test_push_to_talk_needs_no_wake_word` |
| Space (not in a control) | Dock | same | Push-to-talk, no wake word. Hold >400 ms sends on keyup; a tap listens until a pause. | `app.js:1248-1254`; `tests/web/test_web_voice.py` |
| Enter while the mic is hot | Dock | same | Sends the clip now. | `app.js:1249` |
| Ear glyph | Dock | `GET /api/voice/level` | Draws the always-on listener. Does not start it. | `ear.js` |
| Live call | Dock `#agent-call`, hidden until `GET /api/agent` says `configured` | `POST /api/agent/session`, `/api/agent/ask` | ElevenLabs. Esc or click hangs up. Captions in the dock. | `app.js:1347-1350`; `tests/web/test_agent.py` |
| Set up voice | Settings → Voice | `GET/POST /api/voice/setup`, `GET /api/voice/devices` | Guided enroll. Can open itself. | `voicesetup.js` |
| Voice fields | Settings → Voice | `POST /api/settings` | Near-miss floor, Kokoro voice, speed, spoken length, speaker threshold (`voiceprint.json`, not config). | `settings.py:27-35`, `176` |
| Restart voice listener | Notice after a voice setting save | `POST /api/voice` `{action:stop}` then `start` | Cluster-wide restart is still manual. | `settings.js:305-312` |
| Calls: mic, gain, echo, noise, AGC | Settings → Calls | browser only (`jarvis.agentAudio` and related keys) | Saved on this computer. Device and processing apply next call. Gain is immediate. | `index.html:392-409`; `test_audio_preferences_reach_the_live_input` |
| Call voice, speed, steadiness, Preview | Settings → Calls | `/api/agent/voices`, `POST /api/agent/voice`, `POST /api/agent/voice/preview` | Preview plays ElevenLabs or says which engine played. | `app.js:1369-1394` |
| Start / Stop / Restart listener | Connections → On this computer | `POST /api/connections` `{target:here, part:voice, action}` | Start disabled until a voiceprint exists. | `app.js:738-743` |

## Approvals (gate)

`#gate` (`gate.js`, `index.html:469-478`). Always visible on the console. On other hash views it appears only while something is waiting.

| Action | API | Expected | Evidence |
|---|---|---|---|
| Allow (Y) | `POST /api/confirm` `{id, action:approve, hash}` | Runs the parked command. Buttons disable until redraw. | `gate.js:90`, `160` |
| Allow on this site, 10 min (A) | `{action:grant, id, hash}` | Only when `can_grant`. Low-stakes browser verbs. | `ui.py:1080-1088` |
| Decline (N) | `{action:reject, id, hash}` | Counts in the cockpit funnel. | `cockpit.js:61` |
| Stop allowing (× on a grant) | `{action:revoke, site, verb}` | Ends the 10-minute grant. | `ui.py:1071-1077` |
| Recent | none | Local list of answers this page has seen. | `gate.js` |
| Flow Confirm / Reject | same `/api/confirm` | Second surface on the pipeline replay panel (`flow.js:66-81`). Keep both. | source |

Empty copy: “Nothing needs your OK.” Hint names post, buy, send, and unnamed clicks (`index.html:471-472`).

## Files

| Screen / action | API | Expected | Evidence |
|---|---|---|---|
| `#files` open | `GET /api/fs/devices` | One column per laptop that storage knows. | `files.js:154-157` |
| Open folder, up, refresh | `GET /api/fs?device&path` | Directories are buttons. Files are not. | `files.js:50-69` |
| Copy to the other laptop (arrow or drop on the other column) | `POST /api/fs/copy` | SSH transfer. Status line names the result. | `files.js:39-44`; comment at `files.js:1-3` |
| Thumbnail | `GET /api/fs/thumbnail?device&path&v=mtime` | Lazy image, 240×180. | `files.js:56`; `tests/web/test_thumbnails.py` |
| Connections → Send files | `POST /api/send?name&device` (raw body) | Lands in `~/JARVIS-Drop` on the chosen computer. Drag, click, or Enter. | `app.js:755-765`, `913-934`; `test_send_file_stages_upload_under_state_not_tmp` |
| Node card “Send files” | none | Scrolls to the drop zone and selects that computer. | `app.js:818-821` |
| Received files | `GET /api/connections` (drop folder listing) | Read-only list on this computer. | `app.js:746-749` |
| `GET /api/inbox` | no console caller | Server can list a transfer inbox. The page uses the connections drop tile instead. | `ui.py:1604`, `1055-1057` source |

## Tasks and plans

| Surface | Present? | API | Expected | Evidence |
|---|---|---|---|---|
| Vault task board | **No.** `jarvis-memory/memory/jarvis-vault.md` says the console board was not built. No script under `jarvis/web` calls these routes. | `GET /api/vault/tasks?q=`, `POST /api/vault/tasks` `{text,due,priority,note}`, `POST /api/vault/tasks/<id>` `{action,value}`, `GET /api/vault/brief` | Agenda buckets, add, one-line update, morning brief. Publishes `vault.changed`. | `ui.py:297-324`, `1805-1808`; `docs/reference.md:154-157` source |
| Ask “add a task / mark done” | Yes, as a normal ask | `POST /api/ask` → tool brain | Not a form. | `tests/runtime/test_tasks.py` `test_task_requests_route_to_the_tool_brain` |
| `#run` | Yes, from a plan link or the hash | `GET /api/overview`, `GET /api/plans/<id>`, `GET /api/tasks/<id>`, `POST /api/plans/<id>/run` | One plan. “Run this plan” arms to “Confirm: run plan”, then posts `{}`. | `run.js:176-236`; `test_plan_view_reads_real_step_state` |
| CLI `jarvis tasks` | Outside this UI | — | The agenda the missing board would have shown. Do not drop it. | `docs/reference.md:142-151` |

## Vault and knowledge

| Screen / action | API | Expected | Evidence |
|---|---|---|---|
| Settings → Memory “Use my vault” | `POST /api/settings` `vault.recall` | Recall only. | `settings.py:51-52` |
| Settings → Permissions “Write to my vault” | `vault.write` | Additive notes when asked. Default off. | `settings.py:64-65`, `80` |
| Fleet “Memory archive and Git” | `GET /api/fleet/vault` | Read-only. Archive pill and Git block are separate. Copy says JARVIS does not commit or push. `partial` renders “Partly verified” and names plans, runs, and knowledge when `files_stale` or state is `partial`. | `fleet.js:685-744`; `web_fleet_test.cjs` (named in the repair note; not re-run) |
| Knowledge layers Wiki / Vault / Claims | `GET /api/kb/graph`, `/api/vault/graph`, `/api/graph/heap`, `/api/kb/state` | Vault graph failure backs off 10 minutes. | `knowledge.js:1413-1419` |
| Command line | `POST /api/kb/command` `{text}` | `ingest https://…`, `note …`, `ask …`, `check`, `resolve CLAIM keep\|drop\|both`, `merge FROM INTO`, `sync` / `vault`. Local files are not accepted on `ingest`; the server says to drop a file or use `note`. | `ui.py:532-570` |
| Add file / drop | `POST /api/kb/upload?name=` | Text, markdown, HTML, PDF, audio. Up to 200 MB. | `ui.py:581-592`; `knowledge.html:93-94` |
| Resolve buttons in the UI | `POST /api/kb/resolve` | Claim keep/drop against another claim. | `knowledge.js:1256` |
| Graph orbit | none | Drag, scroll, double-click reset, click inspects. Escape clears the scenario. | `knowledge.html:70-71` |
| Scenarios | client highlight | No extra API. | `knowledge.html:55-56` |
| Pipeline panels | `/api/kb/state` + events | Raw (read-only), write queue, latest diff, citations, nightly checker, run log, age, index. | `knowledge.html:98-147` |
| `design.html` is not the vault | — | System design page. Skills section is marked coming soon, three empty slots. | `design.html:109-123` |

C3 vault-primary read is not built. The console must keep showing C2 archive status with `canonical` false. Do not add a general vault editor.

## Connections, fleet, screens

`#devices` stacks four regions (`index.html:343-347`): `#devices-top` (computers and summary), `#fleet-root`, `#peripherals` (Connect & share), `#devices-grid` (this computer, screens, send, personal agents).

| Action | API | Expected | Evidence |
|---|---|---|---|
| Check now | `GET /api/connections` | Polls every 8 s while the view is visible. Skips a repaint during fullscreen or an in-flight upload. | `app.js:640-652` |
| Node presence | connections snapshot | **online** / **busy, awaiting contact** / **offline**. Busy is a job past the 75 s check-in, still inside its deadline. | `app.js:694-701`; `tests/cluster/test_surface_liveness.py` named in the repair note |
| Capability chips | same | Visible text is `c.title`, title attribute is `c.does`. | `app.js:704` source (repair landed) |
| Screen on a node card | none | Selects that computer in the Screens panel. Disabled when the node is not online. | `app.js:706`, `814-816` |
| Screens: tab, Snapshot, Live, Full screen | `GET /api/screen?device=` | **View only.** Live retakes every 3 s while Devices is open and the tab is visible. First visit snaps only a reachable device. | `app.js:825-910` |
| Voice start/stop/restart | `POST /api/connections` | See Voice. | `app.js:806-812` |
| Personal agents | overview `agents` | Read-only rows from config. | `app.js:768-772` |
| Connect & share refresh | `GET /api/peripherals` | Link checks, Lan Mouse start/stop, clipboard start/stop and login autostart, pair side, re-pair / join code. | `peripherals.js:44-119` |
| Laptop WebGL | none | Decoration on the computer cards. No API. | `laptops.js:1-4` |
| Host meters | overview + probe | See Ask section. | `meters.js` |

Fleet (`fleet.js` on `#fleet-root`). Writes only from a console with `can_write`. Otherwise the line is “This computer isn't the MAIN.”

| Section / action | API | Expected | Evidence |
|---|---|---|---|
| Refresh | `GET /api/fleet`, `GET /api/fleet/vault` | Inventory and archive. | `fleet.js:5-6`, `142-149` |
| Save permissions | `POST /api/fleet/capabilities` | Extra capabilities need the acknowledgement checkbox. Camera hint: the node must also run `jarvis camera allow on`. | `fleet.js:299`, `322-341` |
| Revoke | `POST /api/fleet/revoke` `{node, confirm}` | Confirm text must equal the node id. MAIN cannot be revoked here. | `fleet.js:357-377` |
| Check readiness | `POST /api/fleet/preview` | Nothing changes. Shows checks, who-does-what, and commands with a placeholder code. | `fleet.js:547-599` |
| Create / Retry same request / Replace invite | `POST /api/fleet/invite` | One-time code stays in page memory, not `localStorage`. Non-retryable `invite_failed` shows the server sentence and keeps the request until “I checked. Make a new request for it.” Reload forgets that memory; the server still blocks the old id. | `fleet.js:85-92`, `499-624`; `web_fleet_test.cjs` |
| Copy code | clipboard | Fallback selects the code. | `fleet.js:670-675` |
| Move MAIN | `POST /api/fleet/main-transfer/preview` only | “Move MAIN (not available)” stays **disabled**. Check readiness lists gaps. It does not move the MAIN. | `fleet.js:746-760` |
| `POST /api/fleet/main-transfer` | no button calls it | Server `fleet_main_transfer` refuses. | `ui.py:1734-1735` source |

## Remote

`#remote` (`remote.js`). Not the phone camera, and not the Connections snapshot.

| Action | API | Expected | Evidence |
|---|---|---|---|
| List | `GET /api/remote` | Main console only. Cluster off and node consoles get a why and no devices (`ui.py:1205-1209`). | source |
| Pick computer | — | Ready only when presence is online, desktop capability is granted, and code can do remote. Busy shows “Busy, awaiting contact” and is not Ready. | `ui.py:1235-1247`; `remote.js:143-145` |
| Quality low / medium / high | stored `jarvis.remote.quality` | Sent on start. | `remote.js:34-36`, `199` |
| Connect | `POST /api/remote/start` `{device, quality}` | One attached session. Reattach or Stop if one already exists. | `remote.js:149-204` |
| Frames | `GET /api/remote/frame?session&after` | Long-poll JPEG. Stats: fps, resolution, age. | `remote.js:266` |
| Pointer, wheel, keys | `POST /api/remote/input` | Keys go out only while the stage is focused. | `remote.js:43-44`, `326` |
| Disconnect / Stop it | `POST /api/remote/stop` | Also on tab close and after 15 s hidden. | `remote.js:18-22`, `530-531` |
| Fullscreen / Exit | browser Fullscreen API | Exit button inside the session. | `remote.js:37`, `49` |

`tests/desktop/test_controller_pause.py` covers the main keeping its own pointer while a node session runs. Not re-run.

## Camera

There is no camera route, button, or `<video>` in the console shell.

| What exists | Where | Preserve as |
|---|---|---|
| Capability checkbox titled Camera | Fleet permissions. Extra ack. Copy tells the operator to run `jarvis camera allow on` on the node. | A permission, not a viewer. `fleet.js:299` |
| View camera | `jarvis/web/phone/index.html` only. Out of this catalog. | Phone, not console. |
| Screens | Connections, `GET /api/screen` | Still picture of the desktop. |
| Remote | `#remote` | Live desktop plus input. |

## Settings

Sections: Machines, Voice, Calls, Intelligence, Memory, Permissions, Input (`index.html:354-461`). Filter `/` matches label text across sections (`settings.js:155-175`). Each field saves immediately via `POST /api/settings`. Loosening a safety net (`rules.confirm=never`, sandbox off, “stop sharing when apart” off) requires `confirmed:true` after `window.confirm` (`settings.js:219-225`, `settings.py:77`).

| Section | Fields the console can write | Notes |
|---|---|---|
| Machines | Tunnel direction (`cluster.tunnel`). Main PC field is **hidden** (`settings.js:11`). | Machine rows are status, not editors. A non-main row says “MAIN transfer unavailable. See Connections for readiness.” (`settings.js:193`). `settings.write` **rejects** `cluster.main_device` (`settings.py:261-262`). |
| Voice | Near-miss floor, Kokoro voice, speed, spoken length, speaker threshold. | Restart notice + Restart voice listener. |
| Calls | Not config fields. | Browser mic and ElevenLabs voice. See Voice. |
| Intelligence | Brain, interpreter, auto-escalate, decider shadow. | Shadow never acts (`settings.py:46-48`). |
| Memory | Recall by meaning, use vault. | |
| Permissions | Risky actions ask/never, act on computers, browser, Grok Bot, vault write, sandbox, Lan Mouse, stop sharing when apart. | Read-only always-ask and never lists from `settings.py:85-102`. Windows: sandbox is not available here. |
| Input | Shared mouse and keyboard is **read-only**. | `GET /api/input/shared`. Check again. Link to Devices. |
| Input peripherals form | **This browser only.** | `localStorage`. Name, type, connected-to, Add, Remove. Not `/api/peripherals`. `app.js:385-410`; `index.html:452`. |

`POST /api/grokbot` has **no** console caller. The only Grok Bot control is the Permissions switch. The server command helper remains for other clients (`ui.py:1289-1296`).

## Credits

| Action | API | Expected | Evidence |
|---|---|---|---|
| `#credits` and the Usage mini rows | `GET /api/credits`, `?refresh=1` | Sources: live, local logs, entered, not set up, failed. | `app.js:177`, `221-233`; `tests/web/test_ui.py` credit tests; `tests/web/test_credit_dashboard.py` |
| Enter / Update / Remove balance | `POST /api/credits/manual` | Hidden for providers whose setup is `local`. Remove sends `remaining: ""` after confirm. | `app.js:246-259` |
| Check now | refresh=1 | Toast “Balances checked.” | `signals.js:207` |
| DeepSeek Check | same refresh path from the cockpit button | `#bal-check` is wired by `signals.js`. | source |

## System design (two screens)

| Route | What a person can do | API | Expected |
|---|---|---|---|
| `design.html` (rail System) | Trace a sentence (does not run it), zoom the board, run diagnostics, read health, data stores, empty skill slots, module map. | `GET /api/route`, `/api/system/design`, `/api/system/components` | Skills are labeled coming soon. Board needs WebGL; fallback copy is in the page. `design.js`, `board3d.js`. `tests/web/test_sysview.py` |
| `#system` (key 3 only) | Layered import diagram of the repo. | `GET /api/graph/system`, `GET /api/tree` | `system.js`. Not linked from the rail. |

`engine3d.js` draws the console pipeline board from `/api/overview` and `/api/system/components`. It is the console stage, not a separate route.

## Memory view (`#memory`)

| Action | API | Expected | Evidence |
|---|---|---|---|
| Search | `GET /api/memory` | Query, kind, tag, since, minimum importance, “in investigation”. | `app.js:559-572`; `test_search_filters_before_the_limit` |
| Open item | `GET /api/memory/item?id=` | Body, tags, links. Tag click filters. | `app.js:574-595` |
| Link | `POST /api/memory/link` | Relations: related, supports, contradicts, part-of, answers, follows. | `app.js:587-602` |
| Capture | `POST /api/memory` | Kind, title, text, URL, importance, tags, current case. | `app.js:615-625`; case test above |

## Keyboard map (index.html)

| Input | Result |
|---|---|
| 1–9, no modifier, focus not in a control | Hash in the key table above. |
| Enter in `#prompt` | Send, or stop the mic if it is hot and the box is empty. Shift+Enter inserts a newline. |
| Space | Push-to-talk, unless a button, link, or field is focused. |
| Esc | Cancel listen, stop waiting, close inspector, hide overlay. |
| Y / N / A | Gate, when it is showing items (`gate.js` kbd labels). |

## Console API map

Routes the console actually calls, from `ui.py:1572-1808`. Phone `/phone/api/*` is out of scope.

| Method | Path | Caller |
|---|---|---|
| GET | `/api/events` | `app.js`, `knowledge.js`, `engine3d.js` |
| GET | `/api/overview` | cockpit, run, design board |
| GET | `/api/confirm` | `gate.js`, `flow.js` |
| POST | `/api/confirm` | gate, flow |
| POST | `/api/ask` | dock |
| GET | `/api/jobs/<id>` | dock wait |
| GET | `/api/route` | dock preview, `design.js` |
| POST | `/api/listen` | mic (`?wake=1` when always-on) |
| GET | `/api/voice`, `/api/voice/level` | status, ear |
| POST | `/api/voice` | settings restart |
| GET/POST | `/api/voice/setup`, GET `/api/voice/devices` | `voicesetup.js` |
| GET | `/api/agent`, `/api/agent/voices` | call button, Calls |
| POST | `/api/agent/session`, `/api/agent/ask`, `/api/agent/voice`, `/api/agent/voice/preview` | live call and Calls |
| GET/POST | `/api/agent-browser` | agent screen |
| GET/POST | `/api/settings` | `settings.js` |
| GET | `/api/credits`, `/api/usage` | credits, usage |
| POST | `/api/credits/manual` | credits editor |
| GET | `/api/probe` | machines |
| GET/POST | `/api/connections` | devices |
| GET | `/api/screen` | devices screens |
| POST | `/api/send` | drop zone |
| GET | `/api/peripherals` | Connect & share |
| POST | `/api/peripherals` | those switches |
| GET | `/api/input/shared` | Settings Input |
| GET | `/api/fleet`, `/api/fleet/vault` | fleet |
| POST | `/api/fleet/preview`, `/invite`, `/revoke`, `/capabilities`, `/main-transfer/preview` | fleet |
| POST | `/api/fleet/main-transfer` | **no UI** |
| GET/POST | `/api/remote`, `/start`, `/input`, `/stop`; GET `/frame` | remote |
| GET | `/api/fs`, `/api/fs/devices`, `/api/fs/thumbnail` | files |
| POST | `/api/fs/copy` | files |
| GET/POST | `/api/memory`, GET `/api/memory/item`, POST `/api/memory/link` | memory, graph inspector |
| GET/POST | `/api/cases`, GET `/api/export` | header |
| GET | `/api/history`, `/api/history/commands` | thread, replay, activity |
| GET | `/api/graph/heap`, `/api/graph/system`, `/api/tree` | mini-graph, dead full graph, `#system` |
| GET | `/api/plans/<id>`, `/api/tasks/<id>` | run |
| POST | `/api/plans/<id>/run` | run |
| GET | `/api/system/design`, `/api/system/components` | `design.html`, console board |
| GET | `/api/kb/state`, `/graph`, `/page` | knowledge |
| POST | `/api/kb/command`, `/upload`, `/resolve` | knowledge |
| GET | `/api/vault/graph` | knowledge |
| GET/POST | `/api/vault/tasks`, POST `/api/vault/tasks/<id>`, GET `/api/vault/brief` | **no UI** |
| GET | `/api/inbox` | **no UI** |
| GET/POST | `/api/grokbot` | **no UI** (permission switch only) |
| POST | `/api/overlay` | overlay window only |
| GET | `/api/specialists` | registered; no current web caller found |

## Residuals (console only)

Merged with `AUDIT-CONSOLIDATION-SURFACES.md` and `FINAL-SURFACE-REPAIRS.md`. Closed items are not re-filed.

| Sev | What | Where | Repro (source) | Resolution for the parity UI |
|---|---|---|---|---|
| Medium | Settings Machines ignores `presence`. A node that Connections and Remote call “Busy, awaiting contact” is “Offline” here, because the renderer only reads `online`. The payload already includes `presence`. | `settings.js:178-183`; payload `settings.py:232` | Code. Not executed against a live job. Supersedes the repair note’s claim that the Settings **renderer** shows the shared word. | When `presence === "busy"`, show the same “Busy, awaiting contact” sentence. Do not invent a fourth word. |
| Low | Key 3 opens `#system` (`system.js`). The System rail item opens `design.html`. | `app.js:119-125`; `index.html:44` | Press 3 with focus outside a control, then click System. | Keep both screens. Point key 3 at `design.html` only if the owner asks; until then both are in the product. |
| Info | Full `#view-graph` cannot be shown. | `app.js:123-124` | Visit `#graph`. | Knowledge page is the replacement. Mini-graph stays. |
| Info | Vault task agenda and brief have no screen. | `ui.py:1607-1609`, `1805-1808`; no `jarvis/web` caller | Search the web scripts for `vault/tasks`. | Preserve the API and the Ask/CLI path. A new board is new product, not parity. |
| Info | Agent-browser `forward` has no button. | `ui.py:1109`; `index.html:229-232` | The closed button list is back, reload, stop, close. | Preserve those four. |
| Info | `settings.js:261-277` still handles `data-make-main`, but `renderMachines` no longer emits that button. | `settings.js:193` vs `276-277` | The row is a status span. | Leave the write rejected. Do not put the button back. |

Closed in this tree, do not regress:

- Settings does not POST `cluster.main_device`. `write()` raises. Fleet “Move MAIN (not available)” stays disabled.
- Non-retryable invite failures show the server sentence, not “Trying again reuses the same request.”
- Connection chips use titles. Empty copy points at “Connect a computer or server”, not `jarvis cluster invite` (`app.js:706`, `index.html:374`).
- Archive `partial` is “Partly verified”, and file stores are named (`fleet.js:694-699`).
- Linux `bats=missing` does not wipe a present Windows battery on the card (`app.js:444-448`).

## Not built (absence is the feature)

Do not file these as console bugs, and do not draw them as working:

- MAIN lease, gateway, safe handoff, distributed compute. Fleet preview is the only MAIN-move UI, and it cannot move anything.
- C3 vault as the primary read store. SQLite remains. Obsidian C2 write-through is the archive block.
- A console camera viewer.
- A console task agenda.
- Skills on `design.html` (explicitly coming soon).
- Real Dell battery numbers. Fields and fixtures exist for Linux and Windows. This audit did not measure hardware.
- Phone fleet admin. The phone may look. Writes stay on this console.

## Acceptance checks for the next UI

1. Rail destinations and the key table above still open the same screens, including the key-3 / `design.html` split until it is deliberately changed.
2. Ask, always-on mic, push-to-talk, speak-aloud, and the ElevenLabs call still exist as separate controls, with the same APIs.
3. The gate still approves, grants for 10 minutes, declines, and revokes. The flow panel may still confirm.
4. Files still copy between laptops, send into `~/JARVIS-Drop`, and show thumbnails.
5. `#run` still confirms before `POST /api/plans/<id>/run`. Vault task routes stay, even with no board.
6. Fleet still cannot move or revoke the MAIN. Invite, revoke, and extra capabilities keep preview, acknowledgement, and typed revoke.
7. Remote still sends input only while focused, and refuses a busy or offline node. Screens stay view-only. Camera stays off this shell.
8. Settings still cannot write `cluster.main_device`. Loosening a safety net still needs an explicit confirm. The browser-local peripheral list stays local.
9. Archive status stays distinct from Git, and does not say the vault is canonical.
10. No new route, contract, or workflow. Node consoles keep the existing privacy allow-list (`P2-PRIVACY-LAUNCH-FIX.md`); this catalog describes the owner console.

## Limits of this audit

- No browser pass, no server, no microphone, no invite, no remote session, no camera, no vault open.
- No test process. Citations are file names and line numbers in the working tree.
- Dirty-tree count was 231 at audit time, one more than the brief’s “230”.
- Phone DOM and backend invite/archive races belong to the other auditors.
