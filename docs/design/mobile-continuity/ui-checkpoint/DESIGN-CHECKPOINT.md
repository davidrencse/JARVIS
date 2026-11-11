# JARVIS UI checkpoint (unapproved)

Status: design proposal only, 2026-11-11. Nothing in production changes. David approves before any of
this reaches `jarvis/web/`. Mock: `checkpoint.html` in this folder (self-contained, mock data, no
network requests, no backend effects).

Inputs read (read-only, from `/home/jdoe/projects/JARVIS`, branch `implementation/ui-parity-checkpoint-20261111`):
`docs/design/mobile-continuity/PARITY-{CONSOLE,PHONE,API,DESIGN,QA}.md`, `DESIGN.md` §11,
`IMPLEMENTATION-PROGRESS.md`, `jarvis/web/phone/{index.html,phone.css}`, console
`index.html`, `console.css`, `cockpit.css`, plus the frontend-design and impeccable skills
(Operate mode, craft floor). The impeccable context launcher was not rerun (earlier Permission denied);
project context was used directly.

## 1. Direction

Operate mode: a tool David already knows. Inherit the incumbent identity, don't invent one.

- Monochrome. True black / true white page. Colour means state only: amber = waiting on David,
  red = failed. No green for "ok": live is a filled ink dot.
- Geist for text, Geist Mono for numbers, labels that are values (ms, GB, step times, keys), Doto for the
  phone wordmark only. The console wordmark stays Geist Mono 13px, tracked .2em.
- No glass, gradients, eyebrows, decorative caps, old marketing fonts or card grids on the console.
- Remote control is the primary job on the phone; the console's primary job is the Ask dock and the gate.
- Calm legibility: one inverted element per screen (phone: Control screen; console: Send / Allow).

## 2. Tokens (unchanged names; values from PARITY-DESIGN)

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#000` | `#fff` | page |
| `--surface` / `--surface-raised` | `#141414` / `#202020` | `#f3f3f3` / `#e8e8e8` | phone cards, current tab |
| `--ink` / `--ink-2` / `--ink-3` | `#ededed` / `#a8a8a8` / `#8a8a8a` | `#0a0a0a` / `#4a4a4a` / `#6b6b6b` | text levels |
| `--line` / `--line-2` / `--line-ctl` | white .11 / .22 / .32 | black .10 / .20 / .30 | hairlines, control borders |
| `--inv-bg` / `--inv-ink` | `#ededed` / `#000` | `#0a0a0a` / `#fff` | the one primary |
| `--needs` (+`--needs-wash`) | `#f0a640` | `#9a5600` | waiting for David, busy presence |
| `--bad` (+`--bad-wash`) | `#ef7a6c` | `#b3261e` | failure, unreachable |
| `--sans` / `--mono` / `--dot` | Geist / Geist Mono / Doto | same | |
| motion | 180ms `cubic-bezier(.16,1,.3,1)` | | state changes only |
| radius | console 2px; phone controls 12px, cards 16px | | |

Type: console body 14/1.5, title 22/500; phone body 16, title 32/600/-0.035em (29 under 360px).
Fixed scale, no fluid type. Tracking never below -0.04em.

Cleanups proposed (from PARITY-DESIGN findings, for the implementation plan, not done here):
drop dark `--ok #e8e8e8` as a state colour (1.05:1 against ink) in favour of fill/shape; raise
`--jv-faint` (2.82 / 2.61:1) to `--ink-3`; fix `--line-soft` being white in light; make `console.css`
load in the documented order; remove the unused Urbanist preload.

## 3. State truth (one vocabulary across phone and console)

| State | Mark | Words | Never |
|---|---|---|---|
| Live / online | filled ink dot (breathes only if motion allowed) | "Online", "Live" | green |
| Idle / offline | hollow dot | "Offline since 09:12" (time it was last true) | blank or grey-only |
| Stale | dashed hollow dot + "Stale" / "n min old" | "Last seen 2 min ago" | showing old numbers as current |
| Busy presence | amber ring | "Busy, awaiting contact" | painting busy as online or as offline |
| Waiting for David | amber filled dot, amber-bordered row | "Waiting for your OK" | approve on phone |
| Failed | red dot, red text on the failed step only | what failed + what was not done ("Nothing was posted.") | red for the whole trace |
| Not reported / not applicable | text, no bar | "Not reported", "Not applicable" | 0% or a computed percent |

Busy fixes a known residual: phone Devices currently draws busy with the offline dot, and console Settings →
Machines shows Offline for busy. Both should use the amber ring and the busy sentence.

## 4. Components and states

Every interactive component has default, hover, focus-visible, active, disabled, loading, error.

- **Focus:** 2px `--ink` outline, 2px offset, on every control in both surfaces (console today has 1px).
- **Tap targets (phone):** ≥44×44 everywhere; tabs 52px tall; device rows ≥96px; primary actions 88px.
  QA found no phone control under 44px at 320/390/landscape.
- **Disabled:** 50% opacity, `aria-disabled="true"`, and a why-line below in plain words (server text
  where it exists, e.g. `#d-control-why`). Never a silent grey button.
- **Loading:** skeleton rows the shape of the content (no spinners); `role=status` text for screen readers;
  pulse only when motion is allowed.
- **Offline:** phone keeps last data, marks it stale, shows the red in-app banner with Retry (`#net`),
  link readout says "Unreachable". Boot failure still goes to Down, never Pair. Console: live chip
  "Reconnecting" with dashed dot, machine panel says readings are n min old.
- **Errors:** say what failed, what didn't happen, what to do next. Failed trace step red, others normal.
- **Empty:** teach the next step ("None open. Starting one always takes a tap.", "Nothing needs your OK").
- **Reduced motion:** all animation and transitions off under `prefers-reduced-motion: reduce`; the phone
  live dot breathes only under `no-preference` (fixes today's blanket kill).

## 5. Layout

Phone (screen 1): header (mark, Doto wordmark, "on Arch", link dot + ms) → optional `#net` → content →
3-tab dock Devices / Ask / Settings. Device detail order is fixed: Back, status, **Control screen**
(inverted, primary), View camera, why-lines, stats grid (2 columns, 1 under 360px), Picture quality
Low/Medium/High, collapsed Computer details. "Manage computers" is one quiet link under the list and
opens the read-only fleet view.

Breakpoints: ≤359px gutters 16, title 29, main label hidden, meters one column. Landscape with
height ≤520px: tabs become a left rail (as today).

Console (screen 2): rail (≥1181 labelled 196px with 1px ink bar on current; 641–1180 64px icon rail,
inverted current; ≤640 bottom bar) → topbar (22px title, search, Live chip, Replay, Recheck, theme) →
cockpit three columns (rail tabs Machines/Usage/Memory/Activity | stats + state line + Trace |
Assistant) → gate (always present) → Ask dock. ≤1180: work + assistant, rail panel below. ≤760: one
column, assistant first. The gate and dock are in normal flow below the cockpit, so the prompt is never
covered (checked at 1440/1180/720×450).

## 5a. Safe area and keyboard (approach, not device-verified)

Carried from today's phone shell, unchanged: `viewport-fit=cover`, `interactive-widget=resizes-content`,
`--sat/--sab/--sal/--sar` from `env(safe-area-inset-*)`, tabs padded `max(4px, --sab)`, `data-kb=1`
hides the tabs and zeroes `--sab` while a field is focused, `--kb` / `--phone-visible-height` from
`visualViewport` (only counted when an input is focused, scale ≈1, inset >100px), fields 16px to avoid iOS
zoom, key buttons `preventDefault` on pointerdown so the keyboard stays up. The mock uses the same env()
tokens. No physical iPhone or Android was used for this checkpoint; safe-area behaviour is unverified on
hardware.

## 6. Feature-parity map (nothing removed)

Phone (PARITY-PHONE):

| Feature | Where in proposal |
|---|---|
| Routes `#boot #down #pair #/devices #/device/<id> #/control/<id> #/camera/<id> #/ask #/phone #/fleet` | unchanged; Fleet keeps Devices tab current |
| Devices list, Online/Sessions strip, caps, Your sessions, Manage computers | mocked (screen 1, left) |
| Device detail: Control primary, Camera second, stats, quality, details, unknown-id notice | mocked (screen 1, right); unknown-id keeps "Not in the cluster any more" |
| Control session (Stop, Touch/Trackpad, keys, text, release on leave/hide, fps/res/rtt, "No new frames", Start again) | not mocked; keeps current layout, adopts tokens/focus/disabled rules |
| Camera (view only, 15:00, notice, REC over live picture only) | not mocked; unchanged |
| Ask: Send to, 0/2000, Usage & cost, Decision path / How it got there / Where it stopped, Try again, Check again, 30-min resume, "Approve on your PC" | not mocked; unchanged copy, adopts state vocabulary (§3) |
| Settings: name, paired, JARVIS at, main, Theme System/Light/Dark, sessions, Forget (two taps in 4s) | not mocked; unchanged |
| Fleet read-only ("This phone can look, not change", statuses, May/Next, MAIN move unavailable) | not mocked; stays read-only, no action buttons |
| Down / Pair / offline banner / SW network-first | §4 offline rules; shell behaviour unchanged |

Not added: location, maps, phone approvals, MAIN transfer, fleet admin, task board, console camera.

Console (PARITY-CONSOLE):

| Feature | Where |
|---|---|
| Rail 1–9 (Console, Knowledge, System, Credits, Memory, Files, Connections, Remote, Settings) | mocked, same order and keys |
| Header: search memory, Live/Reconnecting, Replay, Recheck, theme cycle, `/` and Ctrl+K | mocked (shortcuts unchanged) |
| Cockpit stats Runs/Memory/Machines/Voice/Brain, state line, Trace, rail tabs, Assistant | mocked |
| Gate: Allow Y, Allow on this site 10 min A, Decline N, grants, Recent, empty copy | mocked ("Waiting for OK" state) |
| Ask dock: mic, ear, prompt, tiers Auto/Quick/Tools/Plan, Send, one job at a time, Stop waiting | mocked ("Busy" state) |
| Read aloud, Clear, live call `#agent-call`, agent screen (Back/Reload/Stop scrolling/Close/Expand), board3d/wheel, funnel, case select, Export | not mocked; kept |
| Files, Vault C2 read-only, Remote (main console only), Connections fleet (Move MAIN disabled) | other views; kept |

## 7. Console contract for the future implementation plan

Restyle in CSS; don't rewrite markup. Keep these IDs (looked up by `app.js` / `signals.js`):
`#page-title #clock-date #top-status #time-pill #hero-search #live-chip #flow-history #hero-refresh
#theme-toggle #view-console #lab-s-* #ck-funnel #ck-t-* #ck-p-* #lab-wheel #lab-state #w-trace #lab-steps
#thread #agent-screen #dock-home #gate #gate-title #gate-hint #gate-grants #gate-hist-btn #gate-list
#gate-recent #dock #trace #mic #ear #agent-call #prompt #tier-select #speak-toggle #send`.
Phone IDs in PARITY-PHONE likewise stay. `tests/test_web_voice.py` constraints (`setAttribute`, no
`dataset`) apply to any JS change. Residuals to fix at the same time: key 3 → `#system` mismatch,
unreachable `#graph`, busy shown as Offline in Settings → Machines.

## 8. Accessibility

Contrast (WCAG ratios; first two rows from PARITY-DESIGN, rest computed for this checkpoint):

| Pair | Dark | Light |
|---|---|---|
| `--ink-3` on page | 6.08 | 5.33 |
| `--needs` / `--bad` on page | 10.23 / 7.67 | 5.67 / 6.54 |
| `--ink-2` on surface | 7.75 | 7.99 |
| `--ink-3` on surface | 5.34 | 4.80 |
| `--needs` on surface (busy sentence in a card) | 8.98 | 5.11 |
| `--ink` on needs-wash / bad-wash | 15.52 / 15.92 | 17.49 / 17.42 |
| `--ink-3` on surface-raised | 4.72 | **4.35** |

Rule from the last row: never put `--ink-3` text on `--surface-raised` (the current tab uses `--ink`).
State is never colour-only: every amber/red mark has words beside it. Dots are `aria-hidden`; the words
carry the meaning. Banners use `role=alert`; loading uses `role=status`. Segmented controls use
`aria-pressed`; rail tabs `aria-selected`.

## 9. QA run for this checkpoint (one batch, one correction)

Preview tools: `preview_status` returned `available:false`, so no in-app browser. Used headless Chrome
(`/usr/bin/google-chrome-stable`, playwright-core from the npx cache) on `file://checkpoint.html`.

Matrix: viewports 320×800, 390×844, 844×390 (phone landscape), 1440×900, 1180×820, 720×450 ×
dark/light × states normal/loading/waiting/busy/offline/error = 72 renders. Checks per render:
`scrollWidth > clientWidth`, any element past the right edge (excluding the scrollable bottom rail at
≤640), phone controls under 44px, prompt top below gate bottom, non-file network requests.

Result: 0 horizontal overflow, 0 elements past the edge, 0 phone targets under 44px, prompt clear of the
gate in all 72, 0 external requests. Screenshots reviewed by eye: 1440 dark "Waiting for OK", 390 light
"Offline".

Correction (the one allowed): in Offline and Busy, Control screen still looked enabled under a "nothing
can start" line. Now `aria-disabled` + 50% in Offline (both actions) and Busy (Control); the mock's View
camera is disabled throughout (Dell has no camera). Re-ran the same matrix: still clean.

Not checked: real Geist/Doto rendering (mock uses local-or-fallback fonts to stay offline), screen
readers, physical phones, 200% browser zoom beyond the 720×450 proxy, the unmocked screens.

## 10. Open questions for David

1. Busy presence: amber ring (proposed) or a hollow dot with the busy sentence only?
2. Console rail panel at 1181+: keep at 260–300px, or narrower to give Trace more room?
3. Should the phone Devices strip stay (Online / Sessions) or drop to a single meta line?
4. Phone live dot breathing: keep (with reduced-motion off) or always still?
