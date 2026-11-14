# Phone design

Direction for `jarvis/web/phone/`: an instrument panel. Geist for text, Geist Mono for readings, Doto for the wordmark only. Colour means state only (live = `--ok`, needs you = `--needs`, fault = `--bad`). A 3 px signal rail on each computer card shows state. One primary action per screen; everything else is quiet and outlined.

## Rules
- Tap targets are at least 44 px, measured on the element's own box (no pseudo-element hit areas). The header wordmark gets `min-height: 44px` with a negative margin so the header height is unchanged.
- Text tokens need 4.5:1 on `--bg`, `--surface` and `--surface-raised` in both themes. Light theme: `--ink-3 #5c5c5c`, `--ok #17693a` (5.4:1 or better on `#e8e8e8`).
- Empty states sit on the composer, left-aligned, and are replaced in place when content arrives (no layout shift).
- Device screen: Control screen is the one filled primary; View camera is outlined, same shape.
- No hard-coded colours in rules. The remote stage is black in both themes, so it uses fixed `--stage-*` tokens.
- "Not reported" readings (a lone wide card from `meters.js`) collapse to one quiet line via `#d-meters .hm-meter.wide:only-child`.

## Contrast check
Relative luminance per WCAG 2.x, `(L1+.05)/(L2+.05)`, for each text token on `--bg`, `--surface`, `--surface-raised`, both themes. Re-run after any token change.

## Screenshots
Matrix: 320, 390 and landscape 844x390, dark and light; screens devices, device, ask, session. Check no horizontal scroll and no target under 44 px.
