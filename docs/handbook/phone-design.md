# Phone app design system

Source: `jarvis/web/phone/phone.css` (block `/* ---- design system ... */` at the end; base tokens at the top).
Look: monochrome, calm, console identity (Geist text, Doto wordmark only). Colour means state, nothing else.

## Direction (design director, 2026-11-13)
- **Personality:** an instrument panel in your pocket. Quiet, exact, never decorative. It reports; it doesn't sell.
- **Signature idea: the signal rail.** Every computer card carries a 3px rail on its left edge: green = live, amber = needs you, grey = idle/offline. It is the only colour on the Devices screen, so a glance answers "what's up". Driven in CSS from the existing `.ld[data-st]` dot (`:has()`), no JS.
- **Type:** Geist for UI (600 titles at `--fs-xl`, -0.02em; 15px body), Geist Mono only for numbers/readouts (ms, fps, counts). Section labels (`.ph h2`) are 11.5px uppercase, tracked, `--ink-3`. Doto stays wordmark-only. System fallbacks: system-ui / ui-monospace.
- **Palette:** neutral greys on black/white; `--ok --needs --bad --info` only for state. Inverted fill (`--inv-bg`) marks the single primary action of a screen; card actions are outlined on `--surface-raised`.
- **Density/rhythm:** 4px grid; 16px inside cards, 12px between cards, 20-24px above a section label. Rows >= 52px, targets >= 44px.
- **Motion:** 120ms press (scale .975), 180ms state changes, 320ms sheets; ease-out in, never bounce. `prefers-reduced-motion` removes transforms and transitions.
- **Icons:** Lucide 24px grid, 2px stroke, `currentColor`, `aria-hidden` with a visible label beside it; tabs and primary actions always pair icon + word.
- **Computer card:** name, plain status line, one outlined action; the rail says the state before you read. Offline names dim to `--ink-2`.
- **Session screen:** the picture owns the screen; chrome is a black frame with mono readouts and one red-outlined Disconnect, always in thumb reach.
- **Ask:** a conversation, not a form. Big calm prompt when empty, the composer pinned above the tabs, approvals always stay on the PC.

## Tokens
- Colour (dark + `[data-theme="light"]`): `--bg --surface --surface-raised --ink --ink-2 --ink-3 --line --line-2 --line-ctl --hover --sunk --track --inv-bg --inv-ink`.
- State: `--ok --needs --bad --info`, each with `-wash` (tinted fill). `--needs-ink` text on needs fill.
- Type: `--fs-xs 11.5 / sm 12.5 / md 15 / lg 18 / xl 24 / 2xl 34`; inputs always 16px (no iOS zoom).
- Space: `--s1..--s10` (4px grid). Gutter `--gut` 22px.
- Radius: `--r-sm 8 / --r-md(--r) 12 / --r-lg 18 / --r-pill`.
- Elevation: `--shadow-1` (cards), `--shadow-2` (sheets, toasts), `--scrim`.
- Motion: `--t-fast 120 / --t-med 180 / --t-slow 320`, `--ease-out`, `--ease-in`. Reduced motion stills everything.
- Safe area: `--sat --sab --sal --sar`; keyboard `--kb`, `--vv-top` (set by phone.js).

## Components
`.btn` (+ `.strong` primary, `.danger`, `.quiet`, `.wide`, `.sm`, `[aria-busy]`), `.mini`, `.ico`, `.seg`,
`.card` (+ `.flat`, `[data-st=needs|bad]`), `.chip[data-st=ok|needs|bad|info]`, `.list`/`.list-row`,
`.field`/`.input` (`[aria-invalid]`), `.toggle` (checkbox), `.toast[data-st=bad]`, `.skel` shimmer,
`.scrim` + `.sheet` (div or `<dialog>`), `.ld` live dot, `.tabs` bottom nav.

## Rules
- Do: 44px targets (use `::after` hit-slop for smaller visuals); one `.strong` per screen; plain-language errors that say what to do next; tokens only.
- Don't: hex colours in screen CSS; colour for decoration; new fonts/frameworks; rename existing tokens/classes (add instead).
- Contrast checked by script: all ink/state tokens >= 4.5:1 on `--bg` and `--surface` in both themes.
