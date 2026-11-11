# Console live checkpoint

Paint-only skin for the JARVIS console. The parent copies this sheet onto `jarvis/web/index.html` and loads it last. This worktree does not edit `index.html`, JavaScript, tests, or the parent tree.

| | |
|---|---|
| Worktree | `/home/jdoe/.t3/worktrees/JARVIS/t3-50ce45e8` |
| Sheet | `/home/jdoe/.t3/worktrees/JARVIS/t3-50ce45e8/jarvis/web/checkpoint.css` |
| Read-only source | `/home/jdoe/projects/JARVIS` (`implementation/ui-parity-checkpoint-20261111`, HEAD `845b558`, dirty tree) |
| Approved mock | `/home/jdoe/.t3/worktrees/JARVIS/t3-9950a573/docs/design/mobile-continuity/ui-checkpoint/` |

Parent wire, after `voicesetup.css` (index.html lines 13–28). `console.css` is not last.

```html
<link rel="stylesheet" href="checkpoint.css">
```

`knowledge.html`, `design.html`, and `phone.css` do not load this file. Phone stays with the other agent.

## Selector map

The mock uses `.con`, `.b-rail`, `.ask`, and `.ib`. Production keeps the IDs `app.js` and `signals.js` already look up. This sheet only restyles those nodes.

| Proposal | Production selectors |
|---|---|
| Page, type, 2px radius | `body`, `.wordmark`, `.page-h`, `--sans` / `--mono` / `--r` |
| Focus ring | `:focus-visible`, `#dock #prompt:focus-visible`, `#hero-search:focus-within`, `#gate :focus-visible`, `#lab-stage:focus-visible`. Remote picture stays `#rm-view` with `outline: none`. Ring moves to `.rm-stage:focus-within`. |
| Live / reconnecting | `#live-chip`, `#live-chip.on .dot`, `#live-chip:not(.on) .dot` |
| Cockpit, stats, tabs | `#view-console .ck-box`, `.ck-stats`, `.ck-tabs button`, `.ck-funnel .needs` |
| Trace | `#view-console .lab-steps .run`, `.fail`, `#trace`, `#trace .step.fail` |
| Assistant | `#thread .msg-body`, `#thread .msg.error .msg-body`, `#listener-log`, `.agent-screen` |
| Gate | `#gate`, `#gate[data-empty="false"]`, `.gate-item`, `.gate-btn.go`, `.gate-grant` |
| Ask dock | `#dock`, `html.dock-in-panel #dock`, `#prompt`, `.seg button[aria-checked="true"]`, `#tier-select`, `.send`, `#mic`, `#ear`, `#speak-toggle`, `#agent-call` |
| Narrow restorations | `@media (max-width: 640px)` on `#live-chip`, `.chip-label`, `#time-pill`, `.case-switch:not([hidden])`, `#dock #speak-toggle` |

Rail widths stay in `console.css`: labeled rail at ≥1181px, 64px icon rail at 641–1180, bottom bar at ≤640. This sheet only makes the ≤640 bar scroll inside `.sidebar` (`overflow-x: auto` on `.rail`, `overflow: hidden` on `.sidebar`).

Cockpit grid, `#gate { position: fixed }`, and dock placement stay in `cockpit.css`, `gate.js`, and `dashboard.js` `placeDock`. Above 860px on the console, `html` gets `dock-in-panel` and `#dock` moves into `#dock-home`. Both placements are styled.

Preserved controls, present in the fixture: rail destinations, `#page-title`, `#clock-date`, `#time-pill`, `#hero-search`, `#live-chip`, `#flow-history`, `#hero-refresh`, `#theme-toggle`, `#view-console`, stats, Machines / Usage / Memory / Activity tabs, `#lab-stage`, `#lab-state`, `#lab-steps`, `#trace`, `#thread`, `#thread-clear`, `#agent-screen` (hidden until JS shows it), `#listener-log`, `#gate`, `#gate-title`, `#gate-hint`, `#gate-grants`, `#gate-hist-btn`, Allow, Decline, `#dock`, `#mic`, `#ear`, `#agent-call` (stays `[hidden]`), `#prompt`, Auto / Quick / Tools / Plan, `#tier-select`, `#speak-toggle`, `#send`. Files, fleet, and remote sample nodes were in the fixture and left `[hidden]` on the console pass.

`--ok`, `--warn`, and `--alert` are not set. Connections and fleet pills keep their existing colors. The engine stage (`.lab-stage`, `.board3d`) is not forced white.

## Parity against the approved mock

Shipped in this sheet:

- Both themes lock the PARITY-DESIGN page tokens. Dark page `#000`, ink `#ededed` / `#a8a8a8` / `#8a8a8a`, needs `#f0a640`, bad `#ef7a6c`, surface `#141414` / `#202020`. Light page `#fff`, ink `#0a0a0a` / `#4a4a4a` / `#6b6b6b`, needs `#9a5600`, bad `#b3261e`, surface `#f3f3f3` / `#e8e8e8`.
- Geist for text, Geist Mono for the wordmark, stats, and the trace. Wordmark is 13px, tracking `.2em`. Title is 22px / 500. Body is 14px / 1.5. Radius is 2px. No glass, no shadow.
- Live is a filled ink dot plus the word Live. Reconnecting is a dashed hollow dot. The dot is not green.
- Waiting is an amber gate border, amber title, and `--needs-wash` on `.gate-item`. Allow (`.gate-btn.go`) and Send are the inverted controls. Decline stays a ghost. Grants are 2px, not pills.
- The selected tier is `--ink` on `--surface-raised`. It is not a second inverted control.
- A failed lab step and `#trace .step.fail` use `--bad`. The assistant error keeps ink text and a `--bad` left border.
- Focus is `2px solid` ink, offset 2px, on `:focus-visible`. `#dock #prompt:focus-visible` beats the older `outline: 0` on the prompt.
- At ≤640 the live chip (including `.chip-label`), time pill, visible case switch, and read-aloud button stay on screen. `#hero-search` loses the 240px min-width. The empty-gate hint stays visible at ≤760 and wraps.
- `#live-chip` and `#dock .seg button` set `transition: none`, so a theme flip does not paint the mid-gray from `app.css` (`.chip` and `.seg button` transition color over `--t`, 180ms).

Left as production behavior, on purpose:

- The gate stays `position: fixed`. `gate.js` sets `--gate-left`, `--gate-right`, `--gate-bottom`, `--gate-h`, `--gate-pad`, and `--ck-top`. The mock’s in-flow gate is the visual target. Un-fixing it would cover the prompt.
- Send stays an icon. `setBusy` only swaps the SVG and the accessible name between Send and Stop waiting. A CSS “Send” label would be wrong while stopping.
- A confirm step is class `run` (“Waiting for your yes”). There is no wait class to paint amber.
- Remote’s own rule at `max-height: 520px` still hides `#dock` and some topbar extras while `#view-remote` is shown. This sheet does not override that.
- `[hidden] { display: none !important }` in `app.css` still wins, except `#dock #speak-toggle` at ≤640, which porcelain hides with `display: none !important`.
- The short-window rule that drops the last `.gate-meta` span stays. It is what keeps 720×450 from covering the prompt.
- Codex and Claude credit rows stay `display: none`. The rings already show those numbers.
- Disabled controls stay at opacity `.5`. That can fall under 4.5:1. It was not measured.
- Doto remains available as `--dot` for surfaces that already use it. The console wordmark and `#view-console .dot-word` use Geist Mono.

## Checks

Static `file://` fixture. No server. Headless Chrome `/usr/bin/google-chrome-stable` via playwright-core at `/home/jdoe/.npm/_npx/381cec31605a419a/node_modules/playwright-core`. The fixture linked the parent sheets in index order, then this worktree’s `checkpoint.css`. An inline copy of `placeDock` and `gate.js` `place()` set the dock class and the gate variables. Contrast is WCAG relative luminance, rounded to 2 decimals. A transparent element background was composited onto the body background. A parent wash was not the backdrop.

Fonts: `document.fonts.check` reported Geist and Geist Mono loaded (`true` / `true`).

### Geometry (first pass, both themes)

Page, stage, and shell `scrollWidth` did not exceed `clientWidth`. Right-edge offenders were empty. Prompt and gate rectangles did not overlap. Nine rail links.

| Viewport | Prompt top–bottom | Gate top–bottom | Dock in panel | Rail link width | Focus `#theme-toggle` |
|---|---|---|---|---|---|
| 1440×900 | 697–737 | 756–890 | true | 161 | `2px solid` ink |
| 1180×820 | 609–649 | 676–810 | true | 40 | `2px solid` ink |
| 720×450 | 391–439 | 132–289 | false | 40 | `2px solid` ink |
| 320×800 | 663–711 | 172–561 | false | 44 | `2px solid` ink |

Ink on that focus ring was `rgb(237, 237, 237)` in dark and `rgb(10, 10, 10)` in light. `#prompt` computed `outline: 2px solid` at all eight console cells.

At ≤860px the dock stays in `.shell` (`dockInPanel` false). At 720×450 the waiting gate is tall (612×157) and sits in the upper viewport. The prompt is below it (top 391, bottom 439) and is not covered. At 320 the light gate measured 296×389.

Visible console controls, dark 1440: mic 76×30, ear 56×30, speak-toggle 38×30, send 40×40, prompt 260×40, live chip 63×32, replay / recheck / theme 44×44, clear 48×26. `#tier-select` is `display: none` at 1440, 1180, and 720 (the segment is the tier control). At 320, light, `#tier-select` is `display: block` 74×32, speak-toggle 38×38, live chip 63×32, send 40×40, prompt 230×48. `#agent-call` stayed `[hidden]` (0×0). Files, fleet, and remote sample nodes were 0×0 on the console pass because their views were `[hidden]`.

Bottom rail at 320, light, after the transition fix: `.rail` `scrollWidth` 428, `clientWidth` 306, last link right 435, window 320. Page overflow stayed false. Settings is reached by scrolling the rail. The same 428 / 306 / 435 reading was taken before the transition fix.

### Contrast (first pass, settled samples)

Same pair at every dark console cell, and at the dark remote cell:

| Sample | Pair | Ratio |
|---|---|---|
| Body, title, wordmark, error | `#ededed` on `#000` | 17.94 |
| Wordmark size / family | 13px Geist Mono | |
| Title | 22px Geist | |
| Body | 14px Geist | |
| Gate hint, stat label | `#8a8a8a` on `#000` | 6.08 |
| Allow, Send | `#000` on `#ededed` | 17.94 |
| Selected tier | `#ededed` on `#202020` | 13.92 |
| Failed step | `#ef7a6c` on `#000` | 7.67 |
| Gate meta | `#a8a8a8` on `#000` | 8.83 |

Light, seven of eight console cells (1440, 1180, 320) plus the samples other than the tier at 720:

| Sample | Pair | Ratio |
|---|---|---|
| Body, title, wordmark, error | `#0a0a0a` on `#fff` | 19.8 |
| Gate hint, stat label | `#6b6b6b` on `#fff` | 5.33 |
| Allow, Send | `#fff` on `#0a0a0a` | 19.8 |
| Selected tier | `#0a0a0a` on `#e8e8e8` | 16.16 |
| Failed step | `#b3261e` on `#fff` | 6.54 |
| Gate meta | `#4a4a4a` on `#fff` | 8.86 |

The first pass still had color transitions. Light `#live-chip` was sampled mid-tween (`rgb(177, 177, 177)`, `rgb(178, 178, 178)`, `rgb(218, 218, 218)`). Those are not the shipped color. Light 720 selected tier was also mid-tween: `#dadada` on `#313131`, ratio 9.31.

### After `transition: none` (second pass, first frame)

Theme was set and `layout()` ran. Color was read with no wait.

| Viewport | Theme | Live chip | Chip ratio on page | Chip transition | Selected tier | Tier ratio | Tier transition |
|---|---|---|---|---|---|---|---|
| 1440×900 | light | `#0a0a0a` | 19.8 | `none 0s` | `#0a0a0a` on `#e8e8e8` | 16.16 | `none 0s` |
| 720×450 | light | `#0a0a0a` | 19.8 | `none 0s` | `#0a0a0a` on `#e8e8e8` | 16.16 | `none 0s` |
| 1440×900 | dark | `#ededed` | 17.94 | `none 0s` | `#ededed` on `#202020` | 13.92 | `none 0s` |
| 320×800 | light | `#0a0a0a` | 19.8 | `none 0s` | `#0a0a0a` on `#e8e8e8` | 16.16 | `none 0s` |

An earlier flip on 1440, before the tier transition was removed, already showed the chip at `rgb(10, 10, 10)` immediately in light and `rgb(237, 237, 237)` in dark, with the dot fill matching that ink (solid, not green). Computed chip transition was `none 0s`.

Lowest settled text ratio in the samples above is 5.33 (light hint and stat label). Disabled opacity was not measured. Gate-meta ratios use the page background. The waiting item’s own background, read once in light, was `rgba(154, 86, 0, 0.09)`. Meta color on that wash was not recomputed.

### Remote, dark 720×450

`#view-remote` shown, console hidden. Page overflow false. Offenders empty. `#dock` children (mic, ear, speak, send, prompt) measured 0×0 because `remote.css` hides `#dock` at this height. `#flow-history` and `#hero-refresh` were `display: none`. `#theme-toggle` stayed 44×44. `#rm-disconnect` 90×30. `#rm-view` 606×128. Focus on `#rm-view` computed `outline-style: none` (Chrome still reports width 3px when the style is none). `#rm-stage` computed `outline: solid 2px`, height 130. The fixture left `#gate` shown (`display: block`, top 283, bottom 440). Production gate visibility on Remote is JavaScript. This pass does not claim the gate hides there.

### Reduced motion

`prefers-reduced-motion: reduce`, dark 1440, after reload. `#live-chip .dot` `animation-name: none`, `animation-duration: 0.001s`. The 0.001s is `app.css` forcing a 1ms duration. The name `none` means the breathe animation is not running. The fixture had no `.skeleton` to measure.

## Limitations

- No physical display, screen reader, or project test suite. The parent runs the suite after it wires the sheet.
- The fixture is representative markup, not a signed-in session. Files and fleet views were not shown, so their buttons were not sized.
- Busy shown as Offline, and key 3 opening `#system` instead of `design.html`, are JavaScript. This sheet does not change them.
- 720×450 with a waiting gate covers the upper area. The prompt stays below the gate. A shorter empty gate was not a separate measured cell.
- The bottom rail at 320 scrolls. Nine 44px links do not fit in 320px. Page-level overflow stayed false.
- Knowledge-graph hues, `--jv-faint`, phone, and the WebGL stage are out of this sheet.
- No new features are drawn: MAIN lease, gateway, handoff, distributed compute, C3 vault-primary, console camera, task board, location, or maps.
