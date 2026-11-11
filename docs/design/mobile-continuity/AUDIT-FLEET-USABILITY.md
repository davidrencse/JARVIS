# Fleet UI usability audit

Date: 2026-11-11. Grok, read-only on production. The only repo file this audit writes is this report. Screenshots and the harness live under `/tmp/jarvis-fleet-ux-grok/`. No UI edit, commit, push, config change, vault write, live DB write, restart, deployment, or real enrollment.

The browser pass used a fake API and an isolated page that mounts `#fleet-root` plus the phone shell. It does not boot the live console. Passing DOM tests do not prove the browser flows, and the browser flows do not prove join, remote control, camera, or failover.

## Findings

Two product findings. The harness also recorded two failed assertions; those are explained under "Harness assertions" and are not findings.

### 1. Medium — light-theme status text is below 4.5:1

- Where: `jarvis/web/porcelain.css:38` (`--ok: #2f7a45`, `--warn: #9a6400` on `--bg: #e7e7e9` at line 17). Painted by `jarvis/web/connections.css:26-28` (`.cx-pill`, 11px, weight 600) and `jarvis/web/fleet.css:66-67` (`.fl-risk` 11px, `.fl-risk.high` uses `--warn`).
- What fails: On the light theme, at 1440, 1180, and 720, computed contrast against `rgb(231, 231, 233)` is 4.26:1 for `.cx-pill.ok` ("MAIN", `rgb(47, 122, 69)`), 4.05:1 for `.cx-pill.warn` ("Offline", `rgb(154, 100, 0)`), and 4.05:1 for `.fl-risk.high` ("high risk", same warn color). WCAG AA for normal text is 4.5:1. Eleven pixels is not large text.
- What still clears: The same three selectors in `data-theme="dark"` measured 16.16:1 (`.cx-pill.ok`, `#e8e8e8` on `#0a0a0a`, the dark override at `porcelain.css:390`) and 10.54:1 (warn / high risk). Light hint, capability, and id samples in `--ink-3` measured 4.61:1 and stayed out of the failure list.
- Minimal fix: Darken the light `--ok` and `--warn` tokens until 11px text on `#e7e7e9` is at least 4.5:1, or paint `.cx-pill.ok`, `.cx-pill.warn`, and `.fl-risk.high` with `--ink`. Keep the dark override as it is.
- Regression: A contrast check, not a screenshot, of those three selectors with `data-theme="light"` at 11px on `#e7e7e9`. Fail the test below 4.5:1.

Evidence class: verified-fake (headless Chrome, fake fleet, both themes, three desktop widths).

### 2. Low — the acknowledgement names the capability id

- Where: `jarvis/web/fleet.js:304`. `ackBox` sets the label to `"I understand " + extras.join(", ") + " go beyond the default..."`. `capTitle()` at line 200 already maps ids to titles and is used on the checkbox row (line 293). The acknowledgement does not call it.
- What the browser showed: With Webcam view ticked, the sentence was "I understand camera go beyond the default and give this computer more control over my data and machines." The checkbox title is "Webcam view" (`jarvis/fleet_management.py:64`). The same path would say `grokbot` for "Grok Bot" and `locate` for "Find where it is". One extra also takes the plural verb "go".
- What still works: The gate holds. Save stayed disabled until the box was ticked (`dellSaveBeforeAck` true, `dellSaveAfterAck` false). Create invite stayed disabled until the invite acknowledgement was ticked (`issueBeforeAck` true, `issueAfterAck` false). After save, the note said the credential did not change and that the node also needs `jarvis camera allow`.
- Minimal fix: Join `extras.map(capTitle)`, and use a verb that agrees for one extra and for several.
- Regression: A DOM test that adds `camera` beyond the profile default and expects the acknowledgement text to contain "Webcam view" and not the bare id. One more case for `locate` → "Find where it is".

Evidence class: verified-fake.

## Evidence classes

### Verified-fake

Headless Chrome, `node /tmp/jarvis-fleet-ux-grok.mjs`, fake HTTP on `127.0.0.1:33371` (closed when the process exited). Report: `/tmp/jarvis-fleet-ux-grok/report.json`. 64 checks, 2 failed (harness assertions below). 39 screenshots. `FLOWCRASH`, `SCENECRASH`, and `CRASH` empty.

Desktop, both themes, isolated `#fleet-root`:

- No horizontal overflow at 1440, 1180, and 720, including a 720 resize of the filled flow (`overflow720` 0).
- PC, headless, and cloud copy. Cloud default is inventory only. Preview text includes "Who does each step", "Not on this MAIN", "JARVIS does not create servers or install an operating system.", and "Ready. Nothing has changed yet."
- Permission acknowledgement gates Save and Create invite.
- Invite code: heading focused, visible commands keep `<INVITE CODE>`, clipboard after Copy join command is the fake code, copy does not POST again, code is absent from `localStorage`, `sessionStorage`, and the URL, and Done removes the code node.
- Lost reply keeps "Retry same request" and the same `request_id`. `already_issued` then Replace uses a new id.
- Pending preview verdict is "Review required. Replace the pending invite only if you intend to cancel its old code." Create stays disabled. The word Ready is absent from that verdict.
- Tunnel copy is "Invites require forward tunnels. Set cluster.tunnel to forward on the MAIN, then check readiness again." No Ready verdict left behind.
- Typed revoke stays disabled for `del`, enables for `dell`, and the typed text survives a refresh. The MAIN row has no revoke.
- A console with `can_write` false disables check, issue, save, and revoke, posts no writes, and says "This computer isn't the MAIN. Do this on the MAIN's console."
- "Move MAIN (not available)" stays disabled. Readiness preview POSTed once. `POST /api/fleet/main-transfer` was not sent. The copy names the missing lease, fencing, and owner proof. That unavailable handoff is planned work, and the screen says so.
- Archive pill reads "Items waiting to be archived" while the fixture's `state` is stale `current`. The archive block says it is separate from Git. Git says unpublished changes, a fetch that never ran, remote not checked, and that a configured remote is not a backup. No commit or push button. Diagnostic counts stay under the collapsed "Archive diagnostic details" disclosure (`fleet.js:687-693`). This pass did not expand that disclosure. The DOM test `backlog_overrides_a_stale_current_archive_label` already matches the pill.
- First load shows "Loading computers…". A failed first load and a failed refresh name that JARVIS could not be reached. Empty fleet has its own sentence.
- Poll: one extra `GET /api/fleet` after a 25s visible clock advance, and no further call after `document.hidden` (`fleet.js:783`, `POLL_MS` 20000).
- Keyboard: after a programmatic focus (outline none, because `:focus-visible` does not match that), Tab through the fields and the eight capability boxes drew `outline: solid 2px`. The 16-stop window reached "Check readiness" with that ring and ended on "How joining works, step by step". "Create invite" is the next control on screen (`connections.css:33` gives `.cx-btn:focus-visible` the same 2px ring). Its own stop was past the window, so that one ring was not measured.

Phone, touch + mobile context, fake `GET /phone/api/fleet` only:

- 390 and 320, landscape 844×390, both themes: overflow 0 on Devices, Manage computers, and the detail. Guide lines include "Who: you", "Who: console on the main", and "Who: you, on the node" (`phone.js:442`). Copy says the phone can look, not change. No locate string, no injected image, no write request. Manage computers sits under the device list. Control screen is above camera. Measured tap heights: Manage 53px, Control 107px, and no sampled control under 44px. Coarse-pointer desktop rules are `fleet.css:137-141` (44px). Fine-pointer desktop buttons stay 30px (`connections.css:30`); that is the desktop size, not a phone miss.
- A long Oracle label clips on the home row (`white-space: nowrap`, scroll width 750, client width 210) and is present in the manage text.
- Empty fleet: "No computers are set up yet. Add one on the main PC, in Connections." A later failed refresh shows "Couldn't refresh, showing the last reading." plus the fake error `boom`.
- Keyboard simulation: `visualViewport` height reduced by more than 100px sets `data-kb="1"` and `.tabs` becomes `display: none` (`phone.css:220`). Safe-area simulation: tab bar `padding-bottom` 34px, top padding 63px, `--sab` 34px. Both are simulations. See Blocks.

A separate T3 preview open of the same harness page rendered "Computers and servers", the three kinds, and overflow 0. That tab was closed. `preview_status` at the end of this audit reports no tab.

DOM tests, re-run for this report, fake DOM only:

- `node tests/web/web_fleet_test.cjs` → 31 PASS, `ok 31`, exit 0.
- `node tests/mobile/web_phone_test.cjs` → 42 PASS, exit 0. The fleet scenarios in that file passed, including read-only GET, Who labels, no location, empty distinct from loading, and no owner write endpoint.
- `JARVIS_NO_MODELS=1 .venv/bin/python -m unittest tests.web.test_web_fleet tests.mobile.test_phone_web -v` → 16 tests, 2.731s, OK.

Node printed `NO_COLOR` ignored because `FORCE_COLOR` is set. That warning is not a failure.

### Verified-live

This usability pass did not call live endpoints. The live facts below are the parent read-only health already recorded in `AUDIT-INTEGRATION.md` and `/tmp/jarvis-independent-live-audit.json`: `/v1/status`, `/api/fleet`, and `/api/fleet/vault` returned 200; zenbook MAIN and dell online; MAIN transfer blocked; archive capture complete, pending 0, held 0, errors 0, state current; cluster and voice active. That is health, not join, remote, camera, or failover proof.

### Failed

No product check in this pass failed. The two red harness assertions are classified in the next section.

The parent full suite was not re-run. Recorded result, unchanged: `JARVIS_NO_MODELS=1 .venv/bin/python -m unittest discover -s tests -q` ran 3334 tests in 243.004s with one error, `web.test_ui_security.ConsoleSecurity.test_oversize_body_gets_413_not_reset` (`BrokenPipeError`; drain cap 1 MiB, body 2.881 MiB). Log: `/tmp/jarvis-independent-audit-suite.log`. An isolated pass of that test does not replace the suite result. It is outside this UI audit.

### Unverified

Listed under Blocks.

## Harness assertions

`CHECKS 64 FAILED 2` in `/tmp/jarvis-fleet-ux-grok/report.json`.

- `archive backlog not current` required the visible string "Waiting to archive" within 40 characters of `2`. Chrome `innerText` of a closed `<details>` omits the body. The screenshot `/tmp/jarvis-fleet-ux-grok/flow-vault.png` shows the pill "Items waiting to be archived", the separation sentence, and a collapsed "Archive diagnostic details". The counts are written into that disclosure at `fleet.js:687-693`. The pill check, the Git-is-not-a-backup check, and the DOM backlog test passed.
- `tab reaches the invite controls` required both "Check readiness" and "Create invite" inside 16 recorded stops. The log reaches "Check readiness" with `outline: solid 2px` and stops on "How joining works, step by step" after eight capability checkboxes. "Create invite" is visible and is the next stop. The first radio's `outline: none` is programmatic focus, which does not match `:focus-visible`.

Console noise, also not product defects:

- `desk-1440-dark` logged a 404. The only missing URL is `/favicon.ico`. The handbook already records one missing fixture.
- `phone-390-dark` logged a 500. The harness fulfills `GET /phone/api/fleet` with status 500 and `{"error":"boom"}` when the stale-refresh scene asks for it (`/tmp/jarvis-fleet-ux-grok.mjs` phone route). The page then showed the last reading and `boom`.

## Prior claims

`docs/handbook/fleet-ui.md:75-78` says the earlier fake-browser pass had no page exceptions and no horizontal overflow at desktop, 320, 390, and landscape, one fixture 404, and that a physical iPhone keyboard and safe area were unverified, with no real enrollment. This pass agrees. Overflow was also 0 at 1180 and 720, which that paragraph does not claim. It does not claim the live console shell was measured. No false claim turned up in that paragraph.

`IMPLEMENTATION-PROGRESS.md` still says the Grok usability audit is pending. This file is that audit. Future work that the screen already labels as unavailable (safe MAIN handoff, phone fleet admin, Oracle authority, lease fencing) is not filed here.

## Blocks

- Physical iPhone, a real `env(safe-area-inset-*)`, and a real soft keyboard. The keyboard and safe-area results above are labeled simulations.
- The owner console shell (sidebar, dock, `app.js`) at 1180 and 720. The harness does not load that shell, so collisions with it are unverified. The live console was not opened.
- Phone first-paint "Loading…" was not caught; the sample ran after boot. Desktop first paint did show "Loading computers…". The phone DOM test `fleet_screen_empty_is_distinct_from_loading` passed.
- Numeric archive rows inside the collapsed disclosure were not expanded in the browser.
- The "Create invite" focus ring itself was outside the 16-stop window. "Check readiness", the same button class, showed the ring.
- No live mutation. No real invite, revoke, permission change, camera, remote control, or MAIN move.

## Browser tooling

`preview_status` at the start of the audit returned `available: false` and no tab. `preview_open` of `http://127.0.0.1:9/` failed with `net::ERR_UNSAFE_PORT` (port 9 is a blocked Chrome port). A later `preview_open` of the running harness succeeded, confirmed the heading and overflow 0, and `t3_preview_close` closed it. Measurement numbers in this report come from the headless Chrome pass, not from that preview tab. A closing `preview_status` reports `available: false`, `tabId: null`. The ephemeral harness server is closed. The owner's Chrome and the T3 preview host process were left running.

## Screenshots

All under `/tmp/jarvis-fleet-ux-grok/`:

- Desktop layout: `desk-1440-dark.png`, `desk-1440-light.png`, `desk-1180-dark.png`, `desk-1180-light.png`, `desk-720-dark.png`, `desk-720-light.png`
- Flows: `flow-secret.png`, `flow-tunnel.png`, `flow-offline.png`, `flow-vault.png`, `flow-transfer.png`, `flow-720.png`, `flow-not-main.png`
- Phone 390/320/landscape, dark and light, devices/fleet/detail; plus `phone-kb-*.png` and `phone-safe-*.png`

Do not cite `/tmp/jarvis-fleet-ux/` (the earlier harness) as this audit's evidence.
