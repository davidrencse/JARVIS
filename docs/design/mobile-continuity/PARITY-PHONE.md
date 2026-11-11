# Phone feature parity (pre-implementation)

Working-tree inventory of the paired-phone PWA. A UI rewrite keeps every row below. It does not add fleet admin, MAIN handoff, a gateway, location, or a second approval path.

**Not production-ready.** This pass did not run the full suite, a live Tailscale phone, a physical iPhone, Dell input, or a camera. Open consolidation defects in `AUDIT-SUMMARY.md` stay open. Green phone tests do not close them.

**Source of truth is the working tree, not HEAD.** Branch `implementation/ui-parity-checkpoint-20261111`, commit `845b55800a2243e826fa5096bbb8520c1ac8dd86`. `git cat-file -e HEAD:jarvis/phone.py` reports the file exists on disk and is absent from that commit. `jarvis/phone.py` and `jarvis/web/phone/` are untracked. `git status --short` listed 231 entries. A diff against HEAD alone drops the phone app.

Scope read: `jarvis/web/phone/{index.html,phone.js,phone.css,sw.js,manifest.webmanifest}` and `jarvis/phone.py`, plus the fleet projection and meter printer those files call. No private store, token, or vault contents were opened.

## Contract that must survive

| Preserve | Dropping it breaks |
|---|---|
| Hash app with the seven routes below | Bookmarks, Back, and the three tabs |
| Boot, unreachable, and pair gates before the app | A failed fetch must not show Pair or clear the cookie |
| Devices list, device detail, Control, Camera | The product is a remote control, not a second JARVIS |
| Ask, decision tree, usage, retry, resume, Send to | Lost replies and reloads |
| Settings: identity, theme, sessions, two-tap forget | Sign-out and theme |
| Manage computers as read-only | Phone is not fleet admin |
| Control as the primary device action, above camera, meters, quality, and details | `IMPLEMENTATION-PROGRESS.md` Remote priority |
| Meters printed from server blocks, under the actions | Battery and host readings |
| Offline shell, in-app banner, fleet last-reading, background session end | Tailscale down or the app backgrounded |
| Safe area, keyboard inset, 16px fields, iOS Home Screen height | Physical iPhone layout |
| No location, radar, Maps, or `navigator.geolocation` | Owner removal, already tested |
| Paired cookie, tailnet checks, no console fall-through | Owner-level remote input |
| Approvals only on the PC | Phone must not approve |

Unbuilt, and not gaps to fill in this UI: MAIN lease, gateway, safe handoff, distributed compute, C3 vault-primary reads, phone fleet writes. Obsidian C2 is the live archive; SQLite stays. Phone code does not read the vault.

## Screens and hash routes

Empty, unknown, or id-less `#/device|control|camera` falls through to Devices (`phone.js` `parse`, lines 486–493). `#/control/<id>` and `#/camera/<id>` do not start a session on load, reload, or Back. A session starts only after a tap sets `want` (`phone.js` 495–506, 570–576).

| Route / screen | DOM | What the user sees | API | Expected | Evidence |
|---|---|---|---|---|---|
| (none) boot `#boot` | Full-screen brand while `#boot` is the only stage | Connecting | `GET /phone/api/me` | 200 enters the app. `401` + `pair:true` goes to Pair. Any other failure goes to Down. Cookie stays. | DOM `a_401_*`, `network_failure_*`, `a_bare_gateway_error_*`. Server `test_me` |
| Down `#down` | Unreachable gate | Tailscale-off copy, or "JARVIS isn't answering" for a silent 502–504, or the server's error text | same `GET /me` | Retry button and a 15s timer while the document is visible. Never Pair. | `phone.js` 179–190, 1541. DOM gateway + network scenarios |
| Pair `#pair` | `#pair-ios` or `#pair-form` or `#pair-wait` | iOS Safari (not standalone): Add to Home Screen, no code form. Else code + name. After a 202: wait for `y` on the main, 5 min bar, Cancel | `POST /phone/pair` then `POST /phone/pair/status` every 2s | 8 letters/digits. Name defaults to iPhone / Android phone / Phone, max 40. `ok` reboots into the app. Net drop while waiting retries status. Cancel tells the user to answer `N`. Desktop OS refused unless the CLI code used `--allow-desktop`. | DOM pair + iOS scenarios. `test_phone.py` pairing/CLI tests. `test_phone_e2e.py` `test_pairing_overview_and_locate` |
| `#/devices` `#s-devices` | Tab Devices | Online/Sessions counts, one row per computer, session list, one Manage computers link | `GET /phone/api/overview` every 5s while visible and no session is starting | Main first, then label. Dot from `online`. Sentence uses `presence === "busy"` → "busy, awaiting contact", else seen / last seen. Caps: screen, camera. Empty copy points at Manage computers. | DOM `devices_screen_has_one_manage_entry_and_remote_stays_first`. `test_overview_*` |
| `#/device/<id>` `#s-device` | Devices tab stays current | Back, status, Control, Camera, meter host, quality, collapsed Computer details | overview payload, no extra route | Unknown id: "Not in the cluster any more", both actions disabled, meters cleared. Own session: Open and End. | `renderDetail` `phone.js` 537–568. Hostile text DOM `hostile_strings_are_drawn_as_text` |
| `#/control/<id>` `#s-control` | Tabs hidden | Live screen, Touch/Trackpad, keys, text, Stop | `POST /phone/api/session/start` `{device,kind:"screen",quality}` then frame + input | See Control actions. Leaving the route releases then stops. | DOM control scenarios. `test_sessions_screen_and_camera` |
| `#/camera/<id>` `#s-camera` | Tabs hidden | Viewfinder, 15:00 countdown, Stop. No keys | start `{kind:"camera"}` + frame. Input is not sent | View only. End sends stop and no `release`. Chrome (REC, clock, cross) only over a live picture. | DOM camera + viewfinder scenarios. e2e camera section |
| `#/ask` `#s-ask` | Tab Ask | Thread, Send to, 0/2000, send | `POST /phone/api/ask`, `GET /phone/api/ask/<job>?after=` | See Ask. Empty state says the phone never approves. | DOM ask scenarios. `PhoneAskTree` |
| `#/phone` `#s-phone` | Tab Settings | Name, paired date, host, main, theme, this phone's sessions, forget | `GET /me` fields; `POST /phone/api/logout` | Two taps within 4s. Success or auth failure returns to Pair. Other errors stay on Settings. | `renderPhone` / forget `phone.js` 913–945. `test_logout_revokes_this_phone` |
| `#/fleet` `#s-fleet` | Not a tab. Devices tab stays current | Read-only computers, guide, "look, not change" | `GET /phone/api/fleet` on entry and every 20s | No invite, revoke, caps, or MAIN control. Locate, addresses, hostnames, and invite codes stripped. | DOM fleet scenarios. `test_phone_fleet.py` |

Tabs (`index.html` 311–315): Devices `#/devices`, Ask `#/ask`, Settings `#/phone`. Three only. Fleet and device/session routes keep the Devices tab `aria-current`.

There is no `#/radar`, `#/locate`, or location screen. `index.html` has no `d-locate`, `d-maps`, `tab-radar`, or `s-radar`. `phone.js` does not call `navigator.geolocation` and does not request `/phone/api/locate`.

## Actions

| Screen | Action | API | Expected |
|---|---|---|---|
| Down | Try again | `GET /me` | Immediate boot, then the 15s timer if it fails again |
| In-app banner | Retry | `GET /overview` | Banner hides when a call succeeds. A lost cookie still goes to Pair |
| Pair | Pair | `POST /phone/pair` `{code,name}` | Client checks `^[A-Z0-9]{8}$` after stripping spaces and dashes. Server uses the unambiguous alphabet and returns 400 for anything else |
| Pair wait | Cancel | none | Stops polling. Does not burn the code. Copy tells the user to type `N` on the main |
| Devices row | Open | hash only | `#/device/<id>` |
| Device | Control screen | start `kind=screen` | Disabled unless `caps.remote` or this phone already has that screen session. Primary button (`phone.css` 578–585, inverted). Why-text from the server, drawn as text |
| Device | View camera | start `kind=camera` | Same rule for `caps.camera`. Second, not primary |
| Device | Open / End session | hash, or `POST /phone/api/session/stop` `{session}` | End refreshes overview even if stop failed |
| Device | Low / Medium / High | `localStorage jarvis.phone.quality` | Sent on the next start only. Default `low`. A running session keeps the quality it started with |
| Device details | (disclosure) | none | Status, role, system, and Wi-Fi / LAN / Tailnet only when that string is non-empty |
| Control | Touch / Trackpad | none; `jarvis.phone.mode` | Touch: tap = left click at the finger, 500ms hold inside 8px = right click, drag = left drag. Trackpad: same clicks at the cursor, drag moves the cursor at gain 1.6. Two fingers: vertical drag = wheel (one notch per 24px, dy clamped −20..20), pinch zooms the picture on the phone only (max 4). Fit clears zoom and must not click the remote. Wheel on the stage scrolls. Context menu and drag-start prevented |
| Control | Keys | `POST /session/input` | Esc Tab Ctrl Alt Shift Super Backspace Enter and four arrows. Modifiers sticky. Meta face is Win / ⌘ / Super from `platform`. Panel open/closed in `jarvis.phone.keys` (default open). Tapping a key does not blur the text field |
| Control | Type / Send | input `{t:"text"}` or Enter | Chunks of 1000. Composition flushed on `compositionend`. Enter with text sends it; empty Enter or empty Backspace is that key. One letter or digit with a sticky modifier is that key chord. Text typed before the first frame is kept and not sent |
| Control | Stop, leave, hide, blur, pagehide | `release` then `POST /session/stop` | Blur and hide release immediately. Hidden 15s ends the session (`HIDDEN_STOP_MS`). `pagehide` uses `keepalive` fetch. bfcache `pageshow` ends locally. Camera stop has no release |
| Control / Camera | Start again | start | Shown only when the session ended or failed |
| Camera | (no pointer input) | frame GET only | A forced input to a camera session is 409 |
| Ask | Send to | stored `jarvis.phone.target` | Options from overview. Offline options disabled. Empty value means main. Selection survives retry via the `target` closed over in `send` |
| Ask | Send | `POST /phone/api/ask` `{text,key,device?}` | Enter sends (Shift+Enter does not). Max 2000. Disabled while a request runs or when `ask.enabled` is false. Placeholder becomes `ask.why` when off |
| Ask | Try again | same POST | PC failure (502, or an error body that carries `job`) uses a new key. Network loss or 409/5xx without `job` reuses the key and the original `device` |
| Ask | Check again | `GET /phone/api/ask/<job>` | After 5 missed polls, or after 20 min. Does not submit a second job |
| Ask thread | Usage & cost | none | `<details>` closed by default. See Costs |
| Ask thread | Decision path | `trace` on ask responses | See Decision tree |
| Settings | System / Light / Dark | `jarvis.theme` and `theme.js` | `data-theme` on `<html>`. Redraws the link trace and device pulses |
| Settings / Devices | Open / End on a session row | same as device session | Both lists share the overview sessions |
| Settings | Forget this phone | `POST /phone/api/logout` `{}` | Second tap within 4s. Clears the cookie (`Max-Age=0`) and ends that phone's sessions |
| Fleet | Try again | `GET /phone/api/fleet` | Shown only after an error. Offline retry keeps the last list and says it could not refresh |
| Header brand | JARVIS | `#/devices` | 44px hit target via CSS |
| Header | Link readout | overview timing | Unreachable / `N ms`. Sparkline canvas is drawn and then hidden (`.link .hb { display: none }`, `phone.css` 442). The visible contract is the dot and the milliseconds |

Input batching (`phone.js` 1185–1229): moves coalesce, other events flush at once, one request in flight, 200 events max, 33ms cadence. A failed batch puts `{t:"release"}` first. One frame long-poll per phone; a second is 429.

## HTTP API

Listener `127.0.0.1:7787` (`phone.port` if 1024–65535). Allowlisted files only (`phone.py` `SHELL` 83–100). No fall-through to console `:7777`. Methods other than GET/POST are 405. Link off, Funnel on this port, or unreadable serve config: 503 for everything.

Every `/phone/*` request: Host is the tailnet name, `name:443`, or loopback with the port; `X-Forwarded-For` is a tailnet address `whois` knows; `Tailscale-User-Login` matches; `Sec-Fetch-Site` is absent, `same-origin`, or `none`. POST also needs `X-JARVIS-Phone: 1`, `Content-Type: application/json`, and a JSON object ≤ 256 KiB.

| Method | Path | Auth | Expected |
|---|---|---|---|
| GET | `/`, `/phone.css`, `/phone.js`, `/sw.js`, `/manifest.webmanifest`, icons, fonts, `/icons.svg`, `/theme.js`, `/esc.js`, `/meters.js` | tailnet checks, no cookie | 200. HTML carries the phone CSP. `sw.js` gets `__SHELL_VERSION__` replaced and `Cache-Control: no-cache`. Fonts/icons/svg: `max-age=86400`. Anything else 404, including `/app.js` and `/phone/api/locate` |
| POST | `/phone/pair`, `/phone/pair/status` | no cookie | 200 + `Set-Cookie` on success, 202 pending, 403 bad/denied/desktop/not-a-phone, 409 code taken, 429 after 5 bad codes / 10 min per IP and per device. Code and cookie never logged |
| GET | `/phone/api/me` | cookie bound to that Tailscale device and login | `{phone:{id,name,created}, host, main:{id,label}}`. No token hash |
| GET | `/phone/api/overview` | paired | `{devices, sessions, ask:{enabled}}`. Device fields: id, label, platform, role, online, presence, seen, caps `{remote,camera}`, why, network `{lan_ip?, tailscale_ip?}`, meters. No `location`, no `locate` cap. Sessions are this phone's only |
| GET | `/phone/api/fleet` | paired | `fleet_management.inventory(..., audience="phone")`. `can_write` false. Write actions `blocked` and have no `path`. No addresses, hostnames, invite secrets, or locate capability. `Cache-Control: no-store` |
| POST | `/phone/api/session/start` | paired | `{device, kind: screen\|camera, quality: low\|medium\|high}`. Opens via the hub as `phone:<id>`. Main allowed only when the target is the main. 400 bad args, 409 taken / hub refusal, 503 no hub. Publishes `phone.session`; camera also `camera.on` (no session id) |
| GET | `/phone/api/session/frame?session&after` | paired opener | JPEG + `X-Frame-Seq/Width/Height`, or 204 + `X-Node-Ago`. 404 unknown (including another phone's id), 410 ended, 429 second poll. One poll per phone |
| POST | `/phone/api/session/input` | paired opener | `{session, events}` ≤ 200. 409 on a camera session. 404/410 as frame |
| POST | `/phone/api/session/stop` | paired opener | Ends it. A forgotten opener id is 404 |
| POST | `/phone/api/ask` | paired | See Ask server rules |
| GET | `/phone/api/ask/<job>?after=` | the phone that submitted it | Others get 404, including a well-formed foreign id. Bad `after` is ignored |
| POST | `/phone/api/logout` | paired | Revokes that phone, ends its sessions, clears the cookie |
| POST | `/phone/api/fleet`, `/phone/api/fleet/*`, `/api/fleet/*` | paired | 404 or 405. No invite, revoke, preview, or MAIN transfer |
| POST | `/phone/api/locate` | paired | **404.** Removed. e2e asserts the locate helper is not called |

Cookie `__Host-jarvis_phone`: HttpOnly, Secure, SameSite=Strict, 90 days, path=/. SHA-256 stored. Works only from the pairing device id and login.

CLI the phone depends on, not a screen: `jarvis phone on|off|status|pair|list|revoke <id>|all`. `on` listens at the next runtime start. `off` refuses at once. `pair` prints the code only on that terminal and requires a plain `y`. Main-only. Covered by `Wiring` tests in `test_phone.py`.

Static shell also includes `/esc.js`. The phone page does not load it (`index.html` scripts are `theme.js`, `meters.js`, `phone.js`). It stays on the allowlist because `sw.js` and `phone.py` `SHELL` must match `test_service_worker_caches_only_the_shell`. Leave it served.

## Feature preservation

### Costs

`request_usage` (`phone.py` 649–662) keeps finite numbers in `tokens`, `usd`, `calls`, `input`, `cached`, `output` (0 to 1e15) plus a redacted `engine`. Secrets and non-finite values are dropped. The client (`phone.js` 789–808) prints API-equivalent USD to 6 places, tokens, calls, and engine. Copy: not an extra subscription charge; missing provider usage can make it incomplete. No usage object: "Usage will be available when the request finishes." while pending, otherwise "Usage was not reported". `calls === 0` and `tokens === 0`: "Local request; no metered model calls." The client does not invent a zero. Test: `test_ask_returns_only_safe_usage_summary`. DOM: `request_cost_is_expandable_and_not_a_subscription_charge`.

### Decision tree

Server `Trace` listens to console `turn.*` only (`phone.py` 665–720). Phone-owned jobs only. Snapshot: rev, tier, reasons, streaming, steps. No events: `available:false` and an empty step list. The client then shows "Working… N s" and draws no tree (`phone.js` 687–768). Steps are labels and short summaries. Tool steps show the tool name, not arguments or output. Failed steps omit detail. `redact` strips tokens, passwords, URL credentials and queries, and deep paths. Caps: 48 jobs, 64 steps, client 80 nodes, nest depth 5. Titles: "Decision path" while running, "How it got there" when done (folded), "Where it stopped" if any step failed (stays open). Poll returns early when `rev` moves, after a 0.15s batch (`ASK_BATCH`). Tests: `PhoneAskTree` in `test_phone.py`. DOM: `ask_draws_the_decision_tree_*`, `ask_without_events_*`, `ask_garbage_trace_*`, `ask_failure_keeps_the_tree_open_*`.

### Retry and resume

Server key is `(phone id, target device, key)` (`phone.py` 1254–1259). Same key and same text returns the first job. Same key on another computer is a new job (`test_ask_retry_key_is_scoped_to_selected_computer`). A key that fails `^[A-Za-z0-9_-]{8,64}$` is ignored. Another phone cannot read the job (`test_a_key_is_scoped_to_its_phone`, `test_slow_ask_is_polled_by_its_own_phone_only`).

Client (`phone.js` 822–905):

| Case | Behavior |
|---|---|
| Network error on POST, or 409/5xx without `job` | "Try again" resends the same key and the same `device` |
| 502, or any error body that includes `job` | "Try again" is a new key. The failed PC request is finished |
| Poll network, up to 5 misses | Keeps the job, backs off, then "Check again" |
| 20 min (`ASK_GIVE_UP_MS`) | "Still working. The answer will show on the PC." Check again polls; it does not resubmit |
| 404 on the job | "JARVIS no longer has that request. Ask again." Saved resume cleared |
| Reload | `localStorage jarvis.phone.ask` holds `{job,text,at,target}` for 30 min. Resume polls. It does not POST. Stale or bad JSON is cleared |

DOM covers same-key lost reply, new-key 502, check-again, and resume-without-a-second-POST. The resume fixture omits `target`. The lost-reply fixture does not assert `device` on the second POST. Both fields are in the client code (`saveAsk` 779, `send` 862–880). Treat target preservation as code-verified, not DOM-asserted.

Confirmation words and `assistant.confirmation_words` are 400 "Approve on your PC" (`phone.py` 1234–1237). A result with `confirmations` is "Waiting for your OK on the PC." Approvals park on the main: `request_from(None)` then `submit(origin_device=target, approval_device=main)` (`phone.py` 1264–1268). Tests: `test_ask_refuses_confirmation_words`, `test_ask_targets_dell_but_approvals_stay_on_main`, e2e `test_ask_runs_as_the_main_and_approvals_stay_on_the_pc`.

### Remote priority

Two different rules. Keep both.

1. **Control stays the primary phone action.** Device screen order (`index.html` 210–241): Control, Camera, meters, quality, collapsed details. Control is the inverted primary button (`phone.css` 578–585). Manage computers is one link under the device list, not a fourth tab (`index.html` 130, 311–315). DOM `devices_screen_has_one_manage_entry_and_remote_stays_first`. This is the "Remote priority" named in `IMPLEMENTATION-PROGRESS.md`.

2. **Ask execution target.** Send to stamps `origin_device`. Empty Send to submits with `origin_device=None` (the main's own request). An explicit device on a desktop command still wins: `Roles.execution_target` is explicit device, else origin, else this PC (`jarvis/fleet/legacy_topology.py` 114–116; applied in `remote.py` `call` around 215–222). The assistant recall line says to use an explicit device id for another PC (`assistant.py` 211–212). The phone does not parse the sentence. The note under Send to (`index.html` 157) states that rule. Phone tests prove the stamp and the approval station. They do not prove a sentence that names another PC overrides the dropdown. That override lives in the existing desktop path. Do not reimplement it inside `phone.js`.

Unknown device: 400. Known node whose `target_caps.remote` is false: 409 with the hub's reason. The main is not passed through that node check.

### Camera

Kind `camera`, qualities low / medium / high. Phone default low. Client countdown 15 min (`CAMERA_MAX_MS`, `phone.js` 19, 1157–1161). The hub also ends camera sessions at 15 min (`tests/mobile/test_camera.py` `test_sessions_end_after_fifteen_minutes`); this pass did not re-run that module. No remote input. The watched computer gets `camera.on` / `camera.off` and a notice. Consent, ffmpeg, infrared skip, and Windows camera privacy are node-side (`docs/handbook/phone.md`). Another holder: 409. Phone UI has no consent switch. Start is a tap, then the hub.

### Pairing

Documented in `docs/handbook/phone.md`. Code TTL 5 min, 60s to collect the cookie after `y`, 5 failures per source per 10 min. iOS outside the Home Screen app is install instructions only, because that storage does not carry into the icon. Server refuses the main itself and any configured cluster computer before it checks the code (`phone.py` 924–940). Cookie is useless from another tailnet device or login.

### Settings

Facts from `/me`: name, paired calendar day, tailnet host, main label. Theme System / Light / Dark. Sessions from this phone with Open and End. Forget is the only write. No shell, no files, no JARVIS settings API. `phone.py` module doc: the phone gets these features and nothing beyond them.

### Fleet read-only

`GET /phone/api/fleet` only. Client also deletes `locate`, invite-shaped tokens, and IPv4-looking strings before display (`publicFleet`, `phone.js` 395–408). Status words: MAIN, Online, Offline, Busy (when `presence === "busy"`), Waiting to connect, Invite pending, Invite expired, Not connected, Not set up, otherwise Unknown. Main row has no capability line. Other rows: "May: …" or "No permissions granted", plus "Next: …" when not online. Guide steps show title, who, and body. Footer says moving the main is not available. Empty list is distinct from "Loading…". Server `can_write` is false and invite/revoke/capabilities/main_transfer are `blocked` with no path (`test_phone_fleet.py`). Phone UI does not render the `actions` array. Do not add buttons for it.

`fleet_management.py` 343–346: for the phone audience, `status` stays the proof word (`online`/`offline`/invite states). `presence` carries the shared liveness word, including `busy`. The row label prefers `presence === "busy"`.

### Meters and offline presence

`#d-meters` calls `JarvisMeters.render` (`meters.js` 35–58). The printer clips blocks (24) and rows (8) and does no arithmetic. Missing meters become one "Stats / Not reported" card. A thrown render leaves the device screen up (`phone.js` 567).

`phone.py` `devices` calls `hostmeter.summarize`. Blocks are RAM, swap, disk, CPU, battery charge, battery health, and a reading stamp (`hostmeter.py` `view`, about 686–718). Battery states the UI must keep showing as sent: Not applicable, Not reported, Unavailable, plus charge and health rows when present. Charge is not health. The phone must not compute a percent.

While a node is `busy`, overview sets `presence` passed into the meter to `None` so the meter does not say Offline during the job (`phone.py` 1089). The device sentence still says "Busy, awaiting contact".

Battery numbers come from Linux sysfs and Windows CIM fixtures. The Dell battery was not measured (`docs/handbook/battery-health.md`, `AUDIT-SUMMARY.md`). Do not treat a fixture as a live Dell reading. Cluster-off SSH probe can still paint a Linux "power_supply is not readable" over a Windows node on the console; phone detail renders `summarize()` only. That split is already filed in `AUDIT-CONSOLIDATION-SURFACES.md` finding 5. Phone must keep printing the overview blocks.

### Offline and reachability

| Layer | Behavior |
|---|---|
| Service worker `sw.js` | Network first for the shell. Cache after 3.5s, or 0.3s if a request already hung (for 10s). 5xx and network errors use the cache. `/phone/*` is never cached. No push, no background sync. Old `jarvis-phone-*` caches drop on activate |
| Boot failure | Down screen. Pair is not shown |
| Later failure | Red banner on the app. Polls that fail set the link to unreachable and do not log the user out |
| `offline` / `online` events | Banner immediately; online boots or refreshes |
| Fleet error | Last list kept, "Couldn't refresh", Try again, meta "stale" after 60s |
| Session | Hide releases input and pauses frames. 15s in background ends it. Six frame failures end it with the lost-Tailscale copy. 6s without a new frame (or a fresh `X-Node-Ago`) is "No new frames". 12s with no first frame is the same |
| Ask | See retry. The job is not cancelled by a dropped poll |

Overview poll pauses while a session is starting or open, and while `document.hidden`.

### Safe area

`viewport-fit=cover` and `interactive-widget=resizes-content` (`index.html` 5). CSS variables `--sat/--sab/--sal/--sar` from `env(safe-area-inset-*)` (`phone.css` 19–22). Applied on the gate, header, tab bar, session dock, and landscape tab rail. Light theme paints a black strip over the status-bar inset (`phone.css` 188) because the Home Screen status bar is light text. Portrait tab padding uses `max(4px, var(--sab))` (588–590). Keyboard open sets `--sab` to 0 (`phone.css` 592) so the home indicator is not reserved twice.

Responsive rules to keep: under 360px hide the header main label and session readouts (`phone.css` 401–405); from 560px the key row is 12 columns; landscape and max-height 520px moves tabs to a left rail and puts the control dock beside the picture (`phone.css` 408–420); meters collapse to one column under 360px. `prefers-reduced-motion: reduce` kills animation (`phone.css` 426–429 and the tree block). Manifest does not lock orientation.

### Keyboard

`viewport()` (`phone.js` 1553–1575) runs on visualViewport resize/scroll, window resize, orientation change, focusin, and focusout.

| Rule | Why |
|---|---|
| Keyboard inset counts only while an input or textarea is focused, pinch scale is about 1, and the inset is over 100px | Safari's bar gap must not be treated as a keyboard. DOM `viewport_gap_is_not_treated_as_a_keyboard` |
| Sets `--kb`, `--vv-top`, `--phone-visible-height`, `data-kb` | Shell bottom and top follow the visible viewport |
| `data-kb=1` hides the tab bar (`phone.css` 220) and drops dock safe-area padding | Room for the keys |
| Installed iOS (`display-mode: standalone` or `navigator.standalone`) sets `data-ios-app=1` and sizes `.app` / `.screen` with an explicit height | Avoids subtracting the keyboard twice. DOM `installed_ios_uses_full_height_and_measured_keyboard_height` |
| Fields are 16px (`phone.css` 161, body 16px at 435) | iOS does not zoom on focus |
| Key and Send `pointerdown` calls `preventDefault` | Soft keyboard stays up |
| Keyboard button focuses the field inside the click | iOS only opens the keyboard from a user gesture |
| `enterkeyhint` send/go/next on the fields | Return key matches the action |

Physical iPhone keyboard and safe area are unverified. `IMPLEMENTATION-PROGRESS.md` already says that. DOM scenarios use a fake viewport, not Safari.

### Local state to keep

| Key | Values |
|---|---|
| `jarvis.phone.ask` | Resume record, 30 min |
| `jarvis.phone.target` | Send-to device id, or empty for main |
| `jarvis.phone.quality` | `low` / `medium` / `high` |
| `jarvis.phone.mode` | `direct` or `track` |
| `jarvis.phone.keys` | `1` open, `0` hidden |
| `jarvis.theme` | `system` / `light` / `dark` (shared with `theme.js`) |

Private-mode storage failures are ignored. They must not break the screen.

## DOM acceptance versus a physical device

Verified this pass: Node DOM harness, 42 scenarios, exit 0. Python wrapper `tests.mobile.test_phone_web.PhoneAppBehaviourTests` runs each scenario and expects `PASS <name>`. The harness stubs `fetch`, timers, and layout. It is not a browser and not a phone.

| Check | DOM / loopback | Physical or live gap |
|---|---|---|
| Pair form vs iOS install gate | User-agent and `standalone` flag | Real Safari vs Home Screen storage, Share sheet, Tailscale |
| Cookie, host, whois, funnel, revoke | Fake tailnet in `test_phone.py` and e2e | Real `tailscale serve` Host and `X-Forwarded-For`. Handbook still says this was not checked on a live tailnet |
| Screen gestures, zoom, keys, text, release | Fake events | Touch slop, iOS callout, remote pointer on Dell or the main. Audit summary: Dell remote input was not exercised |
| Camera viewfinder and "no input" | Fake JPEG | Webcam consent, ffmpeg, Windows privacy, a second holder, 15 min on hardware |
| Ask tree, cost, retry, resume | Fake JSON | A real turn's usage and a reload on iOS Safari |
| Keyboard and safe area | Fake `visualViewport` numbers | iPhone keyboard, home indicator, rotation, VoiceOver. Not measured here |
| Fleet read-only | Fake inventory | Live invite or revoke. Must stay impossible from the phone |
| Meters | Not rendered in the phone DOM scenarios | Linux/Windows fixture shape only. Dell battery unread |
| Busy vs offline copy | `test_overview_busy_node_is_awaiting_contact_not_online_or_offline` | No live job. Cross-screen wording can still diverge; see findings |
| Location | Asserted absent | Must stay absent. No device geolocation test belongs on this app |
| Light/dark, 320/390, landscape overflow | Earlier parent fake-browser note in `IMPLEMENTATION-PROGRESS.md` (0 page exceptions, 0 horizontal overflow, one fixture 404). Not re-run here | Physical iPhone still open |

`docs/handbook/phone-usability.md` (2026-11-09) is a Chromium emulation note, including a Locations tab that this tree no longer has. Current code and `test_location_ui_and_geolocation_removed` win. Do not restore that tab from the old note.

## Findings

Duplicates of open audit items are marked so. New notes are limited to what this read showed.

| Sev | Status | Where | What | Repro | What the rewrite does |
|---|---|---|---|---|---|
| Process | Verified this pass | HEAD `845b558` vs working tree | Phone sources are not in the checkpoint commit | `git cat-file -e HEAD:jarvis/phone.py` → exists on disk, not in HEAD | Build the UI from these files. Do not treat a clean HEAD checkout as the phone |
| — | Already filed, not a phone bug | `AUDIT-SUMMARY.md`, `AUDIT-CONSOLIDATION-BACKEND.md` | Stale full-config write, role change during signing-key generation, oversize task payloads, invite compensation. Suite 3434 green does not cover them | Not re-run | Do not call the phone production-ready. Do not change those paths from the phone UI |
| Medium | Already filed (`AUDIT-CONSOLIDATION-SURFACES.md` §3). Narrower path verified by test | `phone.py` 1067–1070 vs `fleet_management.py` 337–349 | Devices `online` uses the broker check-in when a broker row exists, and falls back to `remote_status` when it does not. That fallback still counts `hub.alive` as online. Fleet `status` for the phone stays the proof word; `presence` is separate. Both UIs say "busy, awaiting contact" when `presence === "busy"` | Broker-row case: `test_overview_busy_node_is_awaiting_contact_not_online_or_offline` (seen 100s, `busy_job` set, `online` false → `presence` `busy`; idle → `offline`). No-broker-row fallback: read of 1067–1070. Live Dell not observed | Keep the busy sentence on both screens. Do not paint `busy` as `online`. Do not invent a second liveness word. The surfaces fix (one presence helper everywhere, including the no-row fallback) is still the open item |
| Low | Verified by reading. No screenshot this pass | `phone.js` 376 and 417; CSS `.ld[data-st="busy"]` at `phone.css` 95 | Device and fleet dots follow `online` only (`live` or `off`). A busy node gets the busy sentence and an offline dot. Nothing in `phone.js` sets `data-st="busy"` | Code inspection. The overview test checks JSON, not the dot | Keep the sentence. Optional: drive the dot from `presence` so busy is not drawn as offline. Do not drop the sentence to match the dot |
| — | Already closed as not a defect | `phone.js` 555–557; `phone.py` 1072–1074; surfaces audit § "Phone device detail" | Wi-Fi row (`#d-r-ssid`) renders only when `network.ssid` is non-empty. `devices()` sends `lan_ip` and `tailscale_ip` only. Overview test expects exactly those keys for the fixture. The row stays hidden | `test_overview_devices_caps_and_locations` | Keep the conditional row. Do not fill it from locate or geolocation. Do not show fleet addresses here; this network object is the configured reachability, and the fleet payload hides addresses on purpose |
| — | Already filed, console path | surfaces audit finding 5; `docs/handbook/battery-health.md` | Cluster-off Linux SSH probe can overwrite a Windows battery on the console. Phone detail does not apply that override. Dell battery unmeasured | Not re-run | Phone keeps `summarize()` blocks. No live Dell figure in this report |
| Low | Code-verified, test gap | `phone.js` 779, 862–880; DOM resume and lost-reply scenarios | Resume stores `target`. Retry passes the captured `device`. The DOM resume fixture has no `target`. The lost-reply scenario checks the key only | Read of `web_phone_test.cjs` 766–809 | Keep sending the original `device` on retry and resume. Add the assertion when tests are next touched. Do not retarget a retry at whatever Send to says now |
| — | Not built, do not file as a regression | `fleet_management.py` 297–298; fleet screen copy `index.html` 149 | Safe MAIN transfer, phone fleet admin, gateway, leases | `test_every_write_action_is_blocked_for_the_phone` | Leave the blocked copy. Do not add a transfer control |
| — | Removed on purpose | `phone.py` 1519–1526; `test_mobile_location_route_removed`; `test_location_ui_and_geolocation_removed`; e2e locate POST | No location feature | Those tests passed this pass | Do not restore radar, Maps, locate, or coordinates on overview or fleet |

Header sparkline: `drawTrace` still runs; `.link .hb` is `display:none`. The visible link is the dot plus milliseconds. Keep the milliseconds. Restoring the canvas is a visual choice, not a missing feature.

## Commands and limits

```sh
.venv/bin/python -m unittest tests.mobile.test_phone tests.mobile.test_phone_e2e tests.mobile.test_phone_web tests.mobile.test_phone_fleet
# Ran 107 tests in 12.829s — OK

node tests/mobile/web_phone_test.cjs
# 42 PASS lines, exit 0
# Node printed: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set.
```

`test_every_scenario` shells the same Node file once per scenario and checks `PASS <name>`. That is included in the 107.

Not run: `unittest discover -s tests`, camera/locate modules as a whole, a dev server, Tailscale, a phone, Dell, paid APIs, or a browser. `phone-usability.md`'s `/tmp/jarvis-mobile-usability.cjs` fixture was not re-run; that note still describes a Locations tab this tree does not have.

Handbook `docs/handbook/phone.md` still points at `tests/test_phone*.py`. The tests live in `tests/mobile/`. Stale path only. `AUDIT-SUMMARY.md` already lists stale implementation docs.
