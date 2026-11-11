# PARITY-DESIGN — visual identity for the Opus checkpoint

Read-only design audit of the working tree on `implementation/ui-parity-checkpoint-20261111` at `845b55800a2243e826fa5096bbb8520c1ac8dd86`, plus the dirty checkout (`git status --porcelain` = 231 entries). This file is the only write. No code, test, config, vault, commit, server, or browser pass.

Not production-ready. The last recorded full suite is 3434 passed, with open consolidation bugs (AUDIT-SUMMARY.md). A green suite does not cover this visual contract. Future MAIN lease, gateway, safe handoff, distributed compute, and C3 vault-primary read are not built. Do not draw them as live controls. Obsidian C2 is the active writer; SQLite stays. Battery fields accept Linux/Windows fixtures; the Dell was not measured.

Impeccable context launcher was not rerun (prior Permission denied). Context is CLAUDE.md, DESIGN.md §11, IMPLEMENTATION-PROGRESS.md, AUDIT-SUMMARY.md, the Operate and craft-floor notes, and the CSS itself. There is no PRODUCT.md. Mode for console and phone is Operate: the person is in a task; brand sits in the wordmark, the hairline, and the type, not in decoration.

## Verdict

Lock one monochrome identity. Geist for interface text, Geist Mono for labels and numbers, Doto for the wordmark only. Page is `#000` / `#fff`. Colour is state and only two hues: amber when JARVIS is waiting, red when something failed. Everything else is ink, gray, a 1px rule, or an inverted fill.

That identity is already what `phone/phone.css` declares and what `console.css` paints on top of older skins. It is not what `porcelain.css` or the marketing site declare. Opus implements the winner below. Opus does not revive Urbanist, frosted glass, 28px pills, or Funnel/Fragment.

## Target tokens

These are the values that win for new console and phone UI. They match the painted console (`console.css` over `app.css` / `porcelain.css`, then `cockpit.css` for `--needs` / `--bad`) and the phone file.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--bg` | `#000` | `#fff` | Page. No gray canvas, no glass. |
| `--ink` | `#ededed` | `#0a0a0a` | Primary text, running, selected. |
| `--ink-2` | `#a8a8a8` | `#4a4a4a` | Secondary text. |
| `--ink-3` | `#8a8a8a` | `#6b6b6b` | Meta, placeholder, the faint floor. |
| `--line` | `rgba(255,255,255,.11)` | `rgba(0,0,0,.1)` | Hairline. |
| `--line-2` | `rgba(255,255,255,.22)` | `rgba(0,0,0,.2)` | Control border. |
| `--inv-bg` / `--inv-ink` | `#ededed` / `#000` | `#0a0a0a` / `#fff` | Primary button, current item. |
| `--needs` | `#f0a640` | `#9a5600` | Waiting on David. Nowhere else. |
| `--bad` | `#ef7a6c` | `#b3261e` | Failed or destructive. Nowhere else. |
| `--sans` | Geist, then system sans | same | Body, buttons, headings. |
| `--mono` | Geist Mono, then ui-monospace | same | Labels, ids, counts, time. Tabular nums. |
| `--dot` | Doto, then Geist Mono | same | Wordmark only. |

Type sizes stay in px, not `clamp`. Console body is 14px/1.5. Phone body is 16px (the later block in `phone.css` wins over 15px). Phone fields stay at least 16px so iOS does not zoom. Phone page title is 32px / 600 / `-0.035em` (inside the `-0.04em` tracking floor). Console page title is 22px/500 Geist.

Radius: console chrome `2px`. Phone controls `12px`. Phone device rows, ask bubbles, and the decision tree are already `16px` (`phone.css` from line 432). Do not flatten the phone to 2px. Do not bring porcelain's 28px cards or 999px nav pills back.

Motion: `180ms` and `cubic-bezier(.16, 1, .3, 1)`. Motion shows state. Reduced motion stops it. Icons are the existing Lucide strokes at 1.6, from `icons.svg` and the phone sprite, plus the five-rectangle mark. No emoji, no new icon set.

Theme: `theme.js` sets `data-theme` before paint. Preference key `jarvis.theme` is `system`, `light`, or `dark`. Default is dark. Console `#theme-toggle` cycles system → light → dark (`app.js` 141–149). Phone Settings uses the same key (`phone.js` 922–928). No theme API.

State marks, in order of preference for anything new:

| State | Mark |
| --- | --- |
| Live / running | Filled ink dot, or inverted label. |
| Idle | Hollow dot. |
| Stale | Dashed dot. |
| Needs a person | Amber `--needs`. |
| Failed / stop | Red `--bad`. |

`app.css` `.src-*` and `phone.css` `.ld` already speak this way. `cockpit.css` lines 7–20 say the same thing in a comment. Porcelain's green `--ok` is leftover, not the target. See findings.

## What actually loads

CLAUDE.md lines 96–97 and the comment in `console.css` line 4 say `console.css` loads last. On disk it does not.

| Document | Stylesheets, in order | Faces preloaded |
| --- | --- | --- |
| `jarvis/web/index.html` 13–28 | `app.css`, `porcelain.css`, `runview.css`, `flow.css`, `sharedinput.css`, `peripherals.css`, `console.css`, `lab.css`, `cockpit.css`, `settings.css`, `files.css`, `connections.css`, `fleet.css`, `laptops.css`, `remote.css`, `voicesetup.css` | Doto, Urbanist, Geist, Geist Mono |
| `knowledge.html` 9–12 | `jarvis.css`, `knowledge.css` | Urbanist, Geist Mono. Geist is used and not preloaded. |
| `design.html` 9–11 | `jarvis.css`, `design.css` | Geist Mono |
| `phone/index.html` 19–22 | `phone.css` only | Geist, Geist Mono. Doto is used and not preloaded. |

`app.css` sets Geist on charcoal `#060708` with glass and a 4px icon rail. `porcelain.css` then sets Urbanist, 28px frosted cards, and a pill rail, and later (line 368) adds Doto plus a second dark palette. `console.css` then sets Geist, `#000`/`#fff`, 2px, hairline sections, and kills glass on `.panel` / `.card`. Later sheets do not replace those root colours. They do restyle their own controls. `cockpit.css` adds `--needs` and `--bad` and is the layout that `#view-console` actually uses.

`phone.css` is two layers in one file. Lines 1–431 are the hairline phone. From line 432 a "Mobile redesign" wins wherever it repeats a property: 16px body, `--surface` `#141414` / `#f3f3f3`, 16px device cards, 32px titles, live-dot animation forced off. Fonts, the Doto wordmark, and the amber/red tokens survive.

`src/styles/tokens.css` is the separate marketing site (Funnel Display, Funnel Sans, Fragment Mono). It is not the console. Do not import it.

## Screens to preserve

Hashes and labels are the navigation. Visual work must not rename, remove, or re-route them. APIs listed are the reads that paint the screen. Writes stay as they are; this audit does not change them.

| Route | Screen | Action the chrome must keep | Data that paints it | Expected look and behavior | Evidence |
| --- | --- | --- | --- | --- | --- |
| `#console` | `#view-console` | Rail, theme, clock, search, gate, assistant prompt | Existing `window.Jarvis.api` via `app.js`. No new call. | Cockpit grid. Ink / gray / amber / red. Prompt not under the gate. | Layout in `cockpit.css` 24–40, 228–252. Browser not opened this turn. |
| `knowledge.html` | Knowledge | Same rail destinations as the console, including this page | Page's own scripts. Unchanged. | Black instrument, Geist Mono 12px. Graph hues stay on this page only. | `knowledge.css` 1–31. Light hairlines: finding 4. |
| `design.html` | System | Same rail | Unchanged | Geist Mono 12.5px, 2px chrome, `jarvis.css` shell | `design.css` 1–18, `jarvis.css` 50–95 |
| `#credits` | Credits | Rail + theme | Unchanged | Console skin, mono figures | Shell in `console.css`. Not remeasured. |
| `#memory` | Memory | Rail + theme | Unchanged | Console skin | Same. |
| `#files` | Files | Rail + theme | Unchanged | Console skin. Folder tones in `files.css` are local. | `files.css` line 3. |
| `#devices` | Connections | Rail. Fleet and invite UI stay. "Move MAIN" stays disabled. | `GET/POST /api/fleet` as today | Status pills use current `--ok`/`--warn`. Do not add a working handoff. | AUDIT-FLEET-USABILITY.md. Contrast test this turn. |
| `#remote` | Remote | Rail. Disconnect releases input. | Unchanged | Console skin, 2px controls | `remote.css`. Behavior is not this audit. |
| `#settings` | Settings | Rail, theme cycle | Unchanged | 2px ink focus on settings controls (`settings.css` 74) | Surfaces audit owns the MAIN button bug. Do not restyle that button into looking safe. |
| `#run` `#graph` `#system` | Hidden views on `index.html` | Keep the nodes. Do not drop them in a layout pass. | Unchanged | They inherit the console skin | `index.html` 251–277 |
| phone boot `#boot` | Connecting | Wordmark, five dots | None until `/phone/api/me` | Centered Doto wordmark on `#000`/`#fff` | `phone/index.html` 49–52 |
| `#down` | Can't reach JARVIS | Try again | Retry `/phone/api/me` | Gate, wifi-off icon, one strong button | `phone/index.html` 55–63 |
| `#pair` | Pair this phone | Code, name, Pair, Cancel. iOS Home Screen steps. | Existing pair flow | 16px inputs, 44px buttons, mono code | `phone/index.html` 67–99 |
| `#/devices` | Your computers | Tabs, Manage computers, device list | `GET /phone/api/overview` | 32px title, 16px device cards, inverted Control is on the detail | `phone.css` 432–478. DOM: `tests/mobile/web_phone_test.cjs` (not re-run). |
| `#/fleet` | Manage computers | Back, read-only list and guide | `GET /phone/api/fleet` only | Phone can look, not change. No locate, no map. | AUDIT-FLEET-USABILITY.md. Code: `phone.js` 401. |
| `#/ask` | Ask JARVIS | Target, thread, send, 2000 count | `POST /phone/api/ask`, `GET /phone/api/ask/:id` | Empty state, 16px field, mono decision tree, red only on a failed step | `phone.css` 537–571 |
| `#/phone` | Settings | Theme segment, sessions, Forget | `POST /phone/api/logout` | Same theme key as the console | `phone.js` 922–942 |
| `#/device/:id` | One computer | Control screen, View camera, quality, facts, meters | Overview plus fleet | Control is the inverted primary. Camera is second. | `phone.css` 578–585, `phone/index.html` 202–228 |
| `#/control/:id` | Remote screen | Stop, Touch/Trackpad, type, keys. A link never starts a session. | `POST /phone/api/session/start`, `input`, `stop` | Stage fills the shell. Tabs hide. Stop is red. | `phone.js` 495–504 |
| `#/camera/:id` | Camera | Stop, 15:00 cap, notice copy | Session APIs, kind `camera` | Viewfinder chrome in mono. Colour is the rec dot and Stop. | `phone/index.html` 286–306 |

Breakpoints already in the CSS, and which a pass has to keep:

| Surface | Width | What changes |
| --- | --- | --- |
| Console rail | ≥1181px | 196px labeled rail, current item is a 1px ink bar (`console.css` 68–77). |
| Console rail | 641–1180px | 64px icon rail. Current item inverts (`console.css` 79–84). |
| Console rail | ≤640px | Bottom bar, 2px, current item inverts (`console.css` 86–90). `jarvis.css` 87–95 matches this for Knowledge and System. |
| Cockpit | ≤1180px | Pipeline beside the assistant, rail moves under them, assistant height stops above the gate (`cockpit.css` 228–240). |
| Cockpit | ≤760px | One column. Assistant, then work, then rail (`cockpit.css` 242–247). |
| Phone | ≤359px | Gutters 16px, title 29px, secondary header text hides (`phone.css` 514–522). |
| Phone | landscape and ≤520px tall | Tabs become a left rail. Control and camera stay one column (`phone.css` 409–422). |
| Phone | keyboard | `data-kb="1"` hides the tab bar. `--sab` goes to 0 (`phone.css` 592, usability audit). |
| Phone | installed iOS | A black strip covers the status inset in light theme too (`phone.css` 186–188). `theme-color` stays `#000000`. |

Checks already recorded, not repeated here: no horizontal overflow at 1440, 1180, 720, 390, 320, and landscape 844×390, both themes, on the fleet harness (AUDIT-FLEET-USABILITY.md). One fixture 404 (`/favicon.ico`). Physical iPhone keyboard and safe area were simulated only.

## Component patterns

| Piece | Console, after the cascade | Phone, after line 432 | Do not |
| --- | --- | --- | --- |
| Wordmark | Geist Mono 13px, tracking `.2em` (`console.css` 66). Knowledge/System match (`jarvis.css` 64). | Doto 700, 21px in the bar, 34px on boot (`phone.css` 137–141, 440). | Put Doto on buttons, labels, or body. |
| Nav current | Ink bar, or inverted tile under 1180px. | Inverted segment / raised tab. `aria-current="page"`. | A sliding pill. Porcelain's `.rail-pill` is display:none in `console.css` 60. |
| Primary button | Inverted fill, 2px. `.cx-btn` is 30px tall on a fine pointer (`connections.css` 30). | `.btn` 48px. `.act` rows taller. Control is the one inverted action. | A second primary on the same phone screen. |
| Ghost / mini | 1px `--line-2`, mono label. | `.mini` at least 44px. | Desktop 30px heights on the phone. |
| Section | Rule, title left, meta right. Console section titles are Geist 13px, not Doto (`console.css` 110). | `h1` then a 15px intro, max about 42ch. | An eyebrow label above the heading. |
| Device / status row | Hairline list, `.state-word` inverted when ok (`app.css` 235–237). | 16px `--surface` card, 108px min row. | Nested cards. |
| Focus | `console.css` 49 paints a 1px ink ring, square. Later sheets use 2px on their own controls. | 2px ink ring, offset 2 (`phone.css` 75). | `outline: none` on `:focus-visible`. |
| Selection, caret, scroll | `app.css` 72–77 themes selection and the scrollbar. Phone sets `caret-color` (`phone.css` 437). | Same idea, thin scrollbar on `.main`. | Leave the browser default blue. |
| Empty / loading | Sentences already in the DOM ("Loading computers…", "No computers are set up yet…"). | Phone empty is a different sentence from an error. | "Nothing here." Spinners in the middle of a list. |
| Reduced motion | Uneven. See finding 6. | Phone sets animation to 1ms globally (`phone.css` 426–429). | A page-load stagger. |

## Accessibility baseline

Measured this turn from the CSS cascade, not from a browser. Command and result are at the end.

| Check | Console cascade | Phone tokens | Knowledge / System |
| --- | --- | --- | --- |
| Body on page | `#0a0a0a` on `#fff`, `#ededed` on `#000`. Far above 4.5. | Same inks. | `--jv-ink` on `--jv-bg`. Same pair. |
| `--ink-3` meta | 5.33 light, 6.08 dark, on the page colour. | Same. On the phone card: 4.80 light (`#6b6b6b` on `#f3f3f3`), 5.34 dark. | `--jv-ink-3` matches. `--jv-faint` does not. Finding 3. |
| `--needs` / `--bad` | 5.67 / 6.54 light, 10.23 / 7.67 dark, on `#fff` / `#000`. | Same tokens. | Not the page accent. |
| State tokens `--ok --warn --alert --info` | Light on `#fff`: 6.18, 6.22, 6.18, 6.73. Dark on `#000`: 17.14, 11.18, 9.20, 12.74. | Phone does not use `--ok`. | Not these tokens. |
| Tap | Desktop buttons may be 30px. That is the fine-pointer size. | 44px floor. Tabs 52px portrait. Usability audit saw no sampled phone control under 44px. | Rail links 34px on a wide desktop, 44px at ≤640px (`jarvis.css` 66, 93). |
| Focus | Visible, but 1px on unscoped controls. Finding 5. | 2px, and heading gets focus on route change (`phone.js` 525–528). | Several controls erase the ring. Finding 5. |
| Names | Rails have `aria-label`. Phone gates use `aria-labelledby`. Live regions exist (`#live`, `role="status"`). | Icon buttons that were checked have names. | Not re-walked control by control. |
| Motion | `prefers-reduced-motion` exists and is inconsistent. | Global kill, plus the live dot is already still. | `knowledge.css` 245 kills all animation. |
| Contrast test | `tests/web/test_theme_contrast.py`, 7 tests, OK. | Not covered by that test. Ratios above are from the same formula. | Not covered. |

Historical light-pill failure is not still true of this tree. AUDIT-FLEET-USABILITY.md measured `#2f7a45` at 4.26 and `#9a6400` at 4.05 on `#e7e7e9`. The file now has `#2a6e3e` and `#865700` (`porcelain.css` 38). Those are 5.00 and 5.04 on `#e7e7e9`, and 6.18 and 6.22 on the white the cascade paints. The old ratios are still asserted inside `test_computation_matches_known_ratios` so the regression stays. This turn did not remeasure computed style in Chrome.

## Findings

| Sev | Where | What | Repro | Resolution for Opus |
| --- | --- | --- | --- | --- |
| Medium | `index.html` 13–28. `console.css` 4. `CLAUDE.md` 96–97. `porcelain.css` 4–9 and 364–395. | Three skins load. The comment says `console.css` is last. Twelve sheets follow it. Urbanist is still declared and preloaded. | Read the `<link>` list. Computed `--sans` on the console is Geist, `--bg` is `#000`/`#fff`, `--r` is 2px. | Treat the target table as the contract. Add rules after `console.css`, or scope them. Do not delete sheets in a visual pass. Do not restore Urbanist or glass. Leave CLAUDE.md for the owner. |
| Medium | `porcelain.css` 390. `connections.css` 26–28. `fleet.css` 67. `tests/web/test_theme_contrast.py` 124–133. | Dark `--ok` `#e8e8e8` against console ink `#ededed` is 1.05:1. AA on black holds (17.14). The distinctness test only samples light porcelain, so a gray "ok" that matches the text still passes. | This turn's token dump. AUDIT-CONSOLIDATION-SURFACES.md already computed 17.14 / 11.18 and 1.05. | New status uses fill or shape (inverted label, filled dot), not a new green. Do not recolor existing pills inside a layout rewrite. A later test can require dark `--ok` to differ from `--ink` by more than 1.5:1. |
| Medium | `jarvis.css` 19 and 41. `design.css` 51, 59, 85, 97. `knowledge.css` 135 and 188. | `--jv-faint` is `#555` on `#000` (2.82) and `#a0a0a0` on `#fff` (2.61). It paints 9–10.5px timestamps, paths, and meta. AA for that size is 4.5. | Formula from `test_theme_contrast.py`, this turn. No browser. | Point faint at `--jv-ink-3` (`#8a8a8a` / `#6b6b6b`, 6.08 / 5.33 on the page). Leave graph cluster colours alone. |
| Low | `knowledge.css` 11. `design.css` 3. | `--line-soft` is white at 6% and 4.5%, including in light theme, where the page is `#fff`. Dividers disappear. Inferred from the declaration. Not screenshotted. | Read the variable. It is not inside a dark-only rule. | Set `--line-soft` from `--jv-line`. |
| Low | `console.css` 49. `knowledge.css` 116. `design.css` 36, 49, 215. | Unscoped console focus is a 1px ring. Knowledge and System set `outline: none` on `:focus-visible` and rely on a border colour change. | Read the selectors. Usability audit did see a 2px ring on `.cx-btn` in the fleet harness. | New and edited controls get `outline: 2px solid var(--ink); outline-offset: 2px`. Do not ship `outline: none` on `:focus-visible`. |
| Low | `connections.css` 39. `phone.css` 426–429 and 513. | Reduced motion slows the invite spinner to 2.4s instead of stopping it. The phone redesign also sets `.ld[data-st="live"] { animation: none }` with no motion query, so the live cue is gone for everyone. | Read both rules. | Stop the spinner under `prefers-reduced-motion`. Keep the live-dot breathe when motion is allowed. |
| Low | `index.html` 10. `knowledge.html` 9. `porcelain.css` 4–7. | Urbanist is preloaded and given `--sans`, then `console.css` 7 replaces `--sans` with Geist. Knowledge preloads Urbanist and never uses it. | Preload href versus `font-family` after the cascade. | Stop preloading Urbanist when load order is next touched. Do not delete `fonts/urbanist.woff2` until nothing names it. |

Not filed again: the light `--ok`/`--warn` AA miss (fixed in this tree; test passes). Phone fleet write actions (absent on purpose). MAIN lease, gateway, handoff, distributed compute, C3 canonical read (not built). Locations or radar (forbidden by DESIGN.md §11.2).

## Preservation constraints

- Keep every route, label, and element id in the screen table. `app.js` and `signals.js` look ids up. Phone sessions start from a tap, never from the hash alone (`phone.js` 502–504).
- Keep copy that states a limit: phone fleet is look-only, Move MAIN is unavailable, archive is not Git, camera shows a notice and stops at 15 minutes, Tailscale is how the phone connects.
- Colour does not decorate. Amber and red are the only hues on console and phone. Knowledge's pink, cyan, and amber are that page's graph language. Do not spread them, and do not strip them while "unifying" the app.
- Do not put a MAIN admin, lease, or compute-queue screen on the phone or in the rail. DESIGN.md §11 describes those as future. A disabled control that already says "not available" stays disabled.
- Do not add a map, a location permission, or a geolocation call.
- Cards exist only where the phone redesign already uses `--surface`. Console sections stay on rules. No icon-plus-heading card grid, no hero metric, no eyebrow, no gradient text, no glass, no hard offset shadow.
- Doto is the wordmark. Geist Mono is for data. Geist is for reading.
- Tracking stops at `-0.04em`. The phone title's `-0.035em` is the tightest allowed.
- Theme stays one key, both apps, default dark, system follows the OS live. The installed phone keeps a black status inset in light mode.
- Desktop density can stay at 30px controls. Phone targets stay at least 44px. Collapsing the rail is the responsive move. Fluid type is not.
- A visual pass does not change API bodies, confirmation rules, or which button is allowed to write.

## Two screens for Opus

Design these two. Do not restyle the other routes in the same step. They already inherit the shell.

### 1. Phone, Your computers and the device

Route `#/devices`, then `#/device/:id`. This is the reference phone.

Keep the Doto wordmark, the link dot, the three tabs, the 32px "Your computers" title, the online/sessions strip, and the 16px device cards. On the device, Control screen stays the single inverted action. View camera stays second and quiet. Facts stay a hairline list. Quality stays a segment. Stop, on the later screens, stays red and at least 48px.

Check both themes at 390, 320, and a short landscape. Title and a long device name wrap or ellipsize inside the card. No horizontal scroll. Tab bar clears the home indicator. With the keyboard flag, the tab bar is gone and the field is still visible. Focus the `h1` on entry. Contrast of meta text on `--surface` stays at least 4.5 (it is 4.80 / 5.34 today).

Do not add fleet admin, a map, or a third primary button.

### 2. Console, the cockpit

Route `#console`, `#view-console`. This is the reference desktop.

Keep the 196 / 64 / bottom rail, the 22px Geist title, the hairline sections, the three-column cockpit, and the gate on top of every view. Assistant prompt stays above the gate at 1180 and at 720×450. Amber appears only where JARVIS is waiting. Red appears only on failure. The theme button still cycles system, light, dark without a flash.

Check dark and light at 1440, 1180, and 720. No new card around the pipeline. No Doto except if a wordmark change is explicit, and the console wordmark today is Geist Mono, so leave it. Connections pills on `#devices` are the token-collision check, not a second brand: leave their colours until a contrast-only patch, and do not sample them as the style to copy.

## Commands and limits

```text
git rev-parse --abbrev-ref HEAD
# implementation/ui-parity-checkpoint-20261111
git rev-parse HEAD
# 845b55800a2243e826fa5096bbb8520c1ac8dd86
git status --porcelain | wc -l
# 231
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest tests.web.test_theme_contrast -v
# Ran 7 tests in 0.022s — OK
```

The contrast figures in this file come from that test's parser plus the same WCAG formula, run in-process against the working tree. No server. No browser. No full suite. No phone or console DOM suite. Physical iPhone, safe area, and a real soft keyboard remain unverified. Prior browser notes in AUDIT-FLEET-USABILITY.md used older light status colours and a fake fleet page, not the live console shell.
