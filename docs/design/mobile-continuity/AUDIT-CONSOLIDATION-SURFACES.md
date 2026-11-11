# Surfaces consolidation audit

Date: 2026-11-11. Read-only. No production, test, config, database, or vault edits. The parent suite in `/tmp/jarvis-repair-battery-combined.log` was not rerun.

Scope is the console, fleet, phone, and meter surfaces, plus the cross-boundary contracts those surfaces display. Server races that do not change a surface are classified below and left to the backend review. Unimplemented MAIN lease, gateway, handoff, and distributed compute are not filed as bugs. Obsidian SQLite-primary (C3) is not done. The Dell battery was not measured. The live vault was not opened. A passing 832-record parity check is not evidence about later writes.

## How this was checked

- Code as it sits in the shared checkout, including archive, fleet, and battery edits already on disk.
- `tests/web/test_theme_contrast.py`: 7 passed.
- `/tmp/jarvis-surfaces-logic.py`: temp config and temp SQLite only. Settings write, liveness, archive verdict, probe-versus-CIM, fleet inventory.
- `/tmp/jarvis-surfaces-ui.cjs`: the existing fake-DOM harness in `tests/web/web_fleet_test.cjs`. No network.
- One browser pass on `http://127.0.0.1:8766/audit.html` (temp static server, fetch stubbed, no live `/api/settings`, no invite, no camera, no keyboard). Tab closed and the server stopped. Headless fallback was not needed.

Evidence labels: **observed** means a repro printed it; **browser** means the preview tab computed or clicked it; **code** means the current file says it and this pass did not execute that path.

## Findings

### 1. High — Settings still changes the main; Fleet refuses

`jarvis/web/settings.js:193-195` renders an enabled "Make main" button. The confirm line is "Hand the brain to {label}?". `settings.js:278-279` calls `save("cluster.main_device", id)`. `settings.js:227` POSTs `{changes: {path: value}}` with no `confirmed` flag. `cluster.main_device` is a normal field (`jarvis/settings.py:21`) and is not in `LOOSENS` (`settings.py:77`), so `write()` does not ask for confirmation. `settings.py:283-293` writes the new id into the config file immediately, leaves the in-memory config on the old main until restart, and returns `restart: ["cluster.main_device"]`. The page then says "Restart JARVIS on every computer to apply."

Fleet does the opposite. `jarvis/fleet_management.py:848-850` documents that `main_transfer` never rewrites `cluster.main_device`. Inventory reports that action as `blocked`. `jarvis/web/fleet.js:749` keeps "Move MAIN (not available)" disabled. This is not the missing lease. It is a second control, on the same console, that performs the rewrite the fleet surface is there to prevent. Restart applies it with no data move and no fence.

**Observed.** Browser click posted exactly `{"changes":{"cluster.main_device":"dell"}}` and showed the restart notice. Temp-file `write(..., confirmed=False)` saved `dell` to disk and left the process config on `zenbook`. Temp inventory: `main_transfer` support is `blocked`. Fake DOM: the Fleet button stays disabled and mount posts nothing.

**Fix.** Remove the button, or make the click show the same "not built yet" text and not POST. `write()` should reject `cluster.main_device` until a real handoff exists. The in-page "Hand the brain" sentence overclaims what the save does.

**Test.** Click `data-ask-main` then `data-make-main` and assert no POST. `settings.write` of `cluster.main_device` raises. Fleet's disabled button stays disabled.

`settings.write` does not take the fleet mutation lock. A save during `issue_invite` can change the file after `fleet_management.py:720` has snapshotted "this PC is still main". That window is the remainder of the old invite/MAIN race. Not executed concurrently here.

### 2. Medium — A non-retryable invite failure is described as a safe retry, and editing the form mints a new request id

`cluster_setup.py:459` now calls `code_key` before `enroll` (`cluster_setup.py:479`). A later failure raises `InviteIncomplete` and rolls the node row and SSH line back only when this attempt still owns them (`cluster_setup.py:496-507`). `retryable` is false when that proof fails. `issue_invite` then stores the request as `uncertain` and returns `invite_failed` with `retryable` false (`fleet_management.py:732-748`). That part of the old "credential before the code, and the same id is always retryable" hole is changed in the current code. Not re-executed against a real broker.

The console throws the distinction away. `fleet.js:39` has one sentence for every `invite_failed`: "Trying again reuses the same request." `fleet.js:85-89` uses that sentence whenever `code` is set, so the server's public detail is not shown. `fleet.js:518` labels the button "Create invite" when `retryable` is false, while `fleet.js:602` keeps the same `request_id` until the form signature changes. `fleet.js:491` drops that id on any edit. The next create is a new id. The uncertain receipt only blocks the old id (`fleet_management.py:682-686`).

**Observed** in the fake DOM, with `POST /api/fleet/invite` stubbed to 409 `invite_failed`, `retryable: false`, message "cleanup could not be verified":

- Visible error contained "Trying again reuses the same request." It did not contain the server detail.
- Button text was "Create invite" and stayed disabled until readiness was checked again.
- The next issue reused `rbea8c456bde5af5a7ecc2ad5`.
- Changing the display name, checking readiness, and issuing sent `ra44a0165383e73f8f4c55f41`.

**Inference, not executed:** if rollback removed the row but could not prove the SSH line, that new id is not stopped by the uncertain receipt. If the row is still there, the server should answer `node_enrolled` or `invite_pending` instead. This pass did not enroll anything.

**Fix.** For `invite_failed` with `retryable !== true`, show the server message (it is already scrubbed) and do not say the retry reuses the request. Keep the request id across label edits until the operator explicitly replaces. The button label should name the id that will be sent.

**Test.** The stub above: error text, button label, same id on recheck, and no new id after a display-name edit.

### 3. Medium — Phone Devices and Remote can say Online while Fleet, Connections, Settings, and the console say Offline

Two clocks:

- Broker `online` is `seen` within `ONLINE` (75s). `cluster_store.py:31`, `cluster_store.py:212`. Fleet `link_state` uses that and nothing else (`fleet_management.py:239-249`). Connections, Settings machines (`settings.py:230`), and the console overview (`ui.py:415`) use it too.
- `Hub.alive` (`cluster.py:577-584`) returns true when `busy_job` is set, before it looks at `online`. The comment says a long job does not poll. `remote_status` (`ui.py:1231`) treats `node.online or hub.alive(...)` as online. Phone Devices (`phone.py:1067`) ORs that with broker online again.

A node last seen 100s ago, with a running job still inside its deadline, is therefore:

| Surface | Word |
|---|---|
| Phone Devices, Remote | Online |
| Phone Manage computers, Fleet, Connections, Settings, console Machines presence | Offline / unreachable |

`alive()` is the right check for "do not fall back to SSH; a job is in flight". It is the wrong bit to paint as "online" on one list and "offline" on the list next to it. The same phone shows both words: Devices uses `remote_status`, Manage computers uses `link_state`.

**Observed.** Temp stub: `seen` age 100, `busy_job` returns a row, `online` false. `hub.alive` true, `link_state` `offline`, the phone-devices predicate true, the console presence predicate `offline`. Not a live Dell observation. No job was running on hardware.

**Fix.** One presence word. Keep `alive()` for dispatch. If a job is in flight past 75s, every surface should say that ("running a job"), not Online on some and Offline on others.

**Test.** One fixture. Assert Fleet, Connections, Settings, console presence, phone Devices, and phone fleet status. They match, or the busy case is an explicit third word on all of them.

### 4. Medium — Archive `partial` is shown as a raw word, and the sentence says the databases are fully captured

`second_brain_runtime._verdict` returns `partial` when database capture is current and `files_stale` is set (`second_brain_runtime.py:450-451`). The comment names plans, runs, and knowledge. `fleet_management._ARCHIVE_KEYS` (`fleet_management.py:877-890`) copies `state` and does not copy `files_stale` or `files`. `fleet.js:686-690` has no `partial` label. The pill text becomes the raw word. The class is `cx-pill` with no warn tone. The sentence beside it is "Capture is enabled for all database stores." whenever `capture_complete` is true, which is true in this state and does not mention the file stores.

**Observed.** `_verdict` on a zero-backlog, capture-complete, `files_stale` record returned `partial`. `files_stale` is not in the allow-list. Fake DOM pill text was `partial`, class `cx-pill`, the database sentence was present, and the text did not mention plans, runs, or knowledge. The existing test only checks that `archive_pending > 0` overrides a stale `current` label.

This is not a C3 bug. The verdict is doing the right thing. The fleet projection drops the reason.

**Fix.** Project a boolean `files_stale` only. Map `partial` to a sentence: database capture is current, and plans, runs, and knowledge have not been verified. Give the pill the warn tone.

**Test.** Vault payload `{state:"partial", capture_enabled:true, capture_complete:true, archive_pending:0, archive_held:0, archive_errors:0}`. Assert the pill is not the raw word and the file stores are named.

### 5. Medium — With the cluster off, a Linux SSH probe replaces a Windows battery reading

Only the non-cluster path. `probe.Prober._probe` (`probe.py:246-256`) does not SSH when the cluster is enabled; it spreads heartbeat records, which have no `meters.blocks`. `app.js:440-443` then keeps the overview `summarize()` view. That is the deployed shape (Zenbook main, Dell node, cluster on). **Do not read this as a live Dell result.** The Dell battery was not measured.

When the cluster is off, `probe_ssh` runs `REMOTE_SCRIPT`, which is Linux `/proc` and `/sys/class/power_supply` (`probe.py:27-68`). No power_supply directory prints `bats=missing`. `from_probe` turns that into battery applicability `unavailable`, reason "power_supply is not readable." (`hostmeter.py:1091-1092`). `remote_summary` always attaches `view(from_probe(...))` (`probe.py:183-185`), so `blocks` is present. `metersForCard` prefers those blocks over the overview for any card that is not `here`. Phone detail does not apply this override; it renders `summarize()` only. The two UIs diverge only in this SSH mode.

**Observed** on constructed readings, no SSH and no Windows CIM call. A CIM canonical sample (charge 80%, health 81.4%, source "Windows CIM") was the overview view. `from_probe({"bats":"missing","host":"WIN-DELL"})` rendered Battery Status Unavailable / "power_supply is not readable." The card picker chose `probe` for a remote card and `overview` for a here card and for a cluster node whose probe payload had no `blocks`.

A Windows OpenSSH login that fails never attaches meters, so the overview stands. The mask needs a login that succeeds and a bash that does not see sysfs (Git bash is the plausible case). That login was not performed.

**Fix.** Do not prefer probe blocks whose battery is unavailable or unreported over a canonical reading that already has a present battery. A missing `power_supply` directory with no `mem_total_kb` is "this script cannot see the machine", not "the battery is unreadable".

**Test.** `bats=missing` and no memory keys, plus a CIM inventory with a present battery. The card's charge row stays 80%. Cluster-on: a heartbeat without `blocks` still shows `summarize()`.

### 6. Low — Connections still shows capability ids and tells the operator to skip the fleet ledger

Same Connections page: Fleet uses titles, an acknowledgement sentence, and `issue_invite` receipts. The node cards do not. `app.js:694` sets the visible chip text to `c.id` and puts `c.does` only in the title. `connections.py:12-14` is where "locate" means "find where it is". The empty states at `app.js:706` and `app.js:714`, the unpaired line at `settings.js:181`, and the Settings footer at `index.html:374` all say to run `jarvis cluster invite`. That CLI calls `cluster_setup.invite` directly. It does not write `cluster-invite-receipts.json`.

**Browser.** The `nodeCard` expression, rendered with the real escaper, showed the text `locatecamera`. The first chip's title was "find where it is". This was the template, not a click through the live connections poll.

**Code, and one temp DB.** Owner inventory kept `locate`. Phone audience inventory of the same row returned `desktop` and `camera` only, and the phone device object had no hostname or address field (`fleet_management.py:336`). `phone.js:407-408` filters `locate` again. The phone shell HTML contains no `geolocation` call. `phone.js` does not reference `geolocation`.

This is not a location leak on the phone fleet screen. It is two vocabularies on the owner console, and a second enroll path beside the form that exists so invites are request-id idempotent. The chip title already has the human sentence, so the id is not a hidden secret. Severity stays low for that reason.

**Fix.** Render `c.does` or the fleet title as the chip text. Point the empty states and the Settings footer at the fleet form. Leave `jarvis cluster invite` as a CLI, not as the instruction next to that form.

**Test.** A node whose capabilities include `locate` and `camera`. Visible Connections text is the title, not the id. Empty-state text does not contain `jarvis cluster invite`.

## Earlier findings, against current code

Not re-run unless the evidence column says so. "Changed" means the cited lines no longer match the old claim.

| Earlier finding | Now | Evidence |
|---|---|---|
| Obsidian: pre-activation rows omitted while status said current | Not retested | Queue still inserts `archive_backfill` and raises `ArchiveCaptureError` (`second_brain_queue.py`). This pass did not enable capture or drain. |
| Obsidian: oversize record wedges later revisions | Changed, not retested | A new write over the cap raises before insert when `origin == "txn"` (`second_brain_queue.py:228-232`). Older rows are held. Not executed. |
| Obsidian: `events.seq` collision across task ledgers | Not retested | `identity-collision` is still an explicit failure (`second_brain.py:842`, `second_brain.py:998`). Restore remap was not re-read and not run. |
| Obsidian: tombstone filename disagrees with the body | Not retested | `second_brain.py:1131` returns `tombstone-flag-disagrees (filename/frontmatter/payload)`. Which side wins was not re-read. |
| Obsidian: docs say the engine is unwired | Changed | `second_brain.py` docstring says C2 is wired and C3 is not. `OBSIDIAN-RUNTIME.md:89` marks the old "fake-backed" sentence as historical and names `partial`. Not every storage doc was re-read. |
| Fleet: tool log from another turn is recorded on success | Changed, not retested | `assistant.py:814-821` and `assistant.py:846` read `turn_log()`, not the shared list. This pass did not race two turns. |
| Fleet: MAIN change during a locked invite still issues a credential | Changed, still a window | `issue_invite` re-reads the config under its lock before `invite()` (`fleet_management.py:718-731`) and then trusts that snapshot. Settings `write` does not take the lock (finding 1). Not run as two threads. |
| Fleet: credential stored before `code_key`; retry always retryable | Changed | `code_key` is before `enroll` (`cluster_setup.py:459`, `cluster_setup.py:479`). Rollback sets `retryable` false when it cannot prove ownership. The UI still mislabels that case (finding 2). Enrollment was not executed. |
| Fleet: request id is only an in-memory ledger | Changed, not retested | Receipts are `cluster-invite-receipts.json` (`fleet_management.py:544-625`). `_LEDGER` is still an in-memory cache beside the file. Restart behavior was not executed. |
| Fleet: cloud row says "gateway-only" | Resolved in the server; fixture stale | Live `main_eligible_why` for a cloud device is "Inventory only by default and never becomes the main." (`fleet_management.py:328`, temp inventory). `tests/web/fleet_fixtures.json:165` still has the old sentence, and the DOM tests replay the fixture, so they do not lock the fix. |
| Usability: light state text under 4.5:1 | Resolved | Contrast test 7 passed, including the cascaded console skin. Browser: light `.cx-pill.ok` is rgb(42,110,62) (`#2a6e3e`) and `.cx-pill.warn` is rgb(134,87,0) (`#865700`); body background is rgb(255,255,255). |
| Usability: ack sentence uses the capability id | Resolved | Fake DOM, extra "Webcam view": "I understand that Webcam view goes beyond the default permissions and gives this computer more control over my data and machines." |
| Oversized upload drops the connection before 413 | Not tested | Not opened. |
| Phone fleet `invite_pending` | Not a defect | Still the status enum. The phone screen has no write. |

Dark pills are a test blind spot, not a contrast failure. `porcelain.css:390` sets dark `--ok` to `#e8e8e8` and `--warn` to `#bdbdbd` after the colored tokens at `porcelain.css:68`. `console.css` does not set state tokens, and it does not load last (`index.html:13-28`). Browser, default dark page: those pills are rgb(232,232,232) and rgb(189,189,189) on body rgb(0,0,0), at 11px/600. Computed ratios on `#000` are 17.14 and 11.18. AA holds. `--ok` against console ink `#ededed` is about 1.05, so Online and the body text are almost the same gray. `test_state_still_reads_as_state_not_ink` only samples light `porcelain.css`, and `test_no_other_stylesheet_redefines_state_tokens` cannot see a later block in the same file. Phone light `--needs #9a5600` on `#fff` is 5.67 at 16px; `--bad #b3261e` is 6.54. Phone dark `--needs #f0a640` on `#000` is 10.23. Not filed.

`fleet.js:718` prints the word "null" if `ahead` or `behind` is not a number while `upstream_set` is true. **Observed** only by forcing null into the fake vault. The API sets `upstream_set` from a successful count parse and sets both numbers to null on `ValueError` (`fleet_management.py:1002-1007`). Not filed. If that parse fails, the line should say unknown, not "null".

## Keep, consolidate, remove

- **Remove** the Settings "Make main" write (finding 1). **Keep** the disabled Fleet control and `main_transfer` blocked.
- **Keep** `Hub.alive` for dispatch. **Do not keep** a second online definition for Remote and phone Devices (finding 3).
- **Keep** `meters.js` as a printer. It does not compute charge. **Keep** `summarize()` as the cluster-on reading. **Stop** preferring a Linux SSH `view()` over a present canonical battery (finding 5). The `.hm-*` rules in `connections.css:74-88` and `phone/phone.css:643-654` match, except the phone host has `margin-top: 8px`. Leave them until a property changes meaning.
- **Keep** both polls that hit different data: Fleet 20s, vault 60s (server cache 15s), connections 8s, overview 20s, probe 60s (server cache 55s), phone devices 5s, phone fleet 20s. Not worth merging.
- **Keep** the phone service worker shell (`phone/sw.js`): `meters.js`, `theme.js`, `esc.js`, `phone.css`, `phone.js`, fonts, icons. It does not cache `fleet.js` or `/phone/*`. No missing shell file was found.
- **Keep** hardware spec (model, CPU, RAM text) and usage meters as different facts. Do not merge them into one RAM number.
- **Update** `tests/web/fleet_fixtures.json:165` so a green DOM run cannot preserve the retired gateway sentence.

## What is working

- Fleet acknowledgement uses the capability title and "goes" / "go". **Observed.**
- "Move MAIN (not available)" stays disabled. Mount does not POST. **Observed.**
- Phone fleet audience: `can_write` false, write actions blocked, `locate` removed, no hostname or address field on that payload. **Observed** on a temp database. Browser: Control screen is before View camera in `phone/index.html`. The shell does not call `geolocation`.
- Phone device detail still shows configured LAN, tailnet, and Wi-Fi when the devices payload has them (`phone.js:553-556`). That is the owner's reachability for that computer, from `phone.py` `network`, not the locate grant and not the fleet payload. Sensible split. Not filed.
- Archive pill replaces a stale "current" when `archive_pending > 0`. Covered by `web_fleet_test.cjs`. Git copy is a separate block and says JARVIS does not commit or push.
- Cluster-on probe does not replace `summarize()` with the Linux script. **Observed** on the card picker.
- `meters.js` prints backend blocks and does not invent a percentage. One battery's charge, health, capacity, degradation, cycles, and reading rows stay inside the 8-row clip on the CIM sample used above (health card had 6 rows).
- Light and dark state tokens pass the cascaded 4.5:1 test. Phone needs/bad tokens, computed above, also clear 4.5:1 on `#fff` and `#000`.
- A successful drain clearing `last_error` is in `second_brain_runtime.py` (the old `has_error` + `current` contradiction). Not re-drained here. `files_stale` is the hole that remains, and it is finding 4.

## Projection versus drift

| Duplicated logic | Verdict |
|---|---|
| Fleet titles vs phone fleet titles | Projection. Phone is read-only and strips locate. |
| Phone device LAN/tailnet vs fleet address hiding | Projection. Different payloads. |
| Settings main write vs Fleet refusal | Drift. Finding 1. |
| `alive()` vs `link_state` | Drift. Finding 3. |
| Archive verdict vs fleet pill | Drift. Finding 4. |
| `summarize()` vs SSH `from_probe` | Drift only when the cluster is off. Finding 5. |
| Connections chip id vs fleet title | Drift. Finding 6. |
| `.hm-*` in two stylesheets | Same meaning today. Leave it. |
| Porcelain dark tokens declared twice | Later block wins. AA still holds. Blind spot in the hue test. |

## Not claimed

- No live invite, no live settings save, no camera, no keyboard, no Windows or Dell battery reading.
- C3 canonical read, MAIN lease, gateway, and distributed compute are not implemented and are not bugs in this list.
- Personal-data SQLite files were not opened or migrated.
