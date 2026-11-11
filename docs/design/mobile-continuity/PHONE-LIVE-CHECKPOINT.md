# Phone live checkpoint

Additive stylesheet only. It is not linked, not in the service-worker shell, not served, and not committed. Parent phone files were read and not modified.

| | |
|---|---|
| Worktree | `/home/jdoe/.t3/worktrees/JARVIS/t3-6cb1c157` |
| Stylesheet | `/home/jdoe/.t3/worktrees/JARVIS/t3-6cb1c157/jarvis/web/phone/checkpoint.css` |
| Read-only phone | `/home/jdoe/projects/JARVIS/jarvis/web/phone/` on `implementation/ui-parity-checkpoint-20261111` at `845b558` |
| Approved mock | `/home/jdoe/.t3/worktrees/JARVIS/t3-9950a573/docs/design/mobile-continuity/ui-checkpoint/checkpoint.html` |

`checkpoint.css` must load after `phone.css`. It uses tokens, the `breathe` keyframes, and the safe-area variables from that file. It has no `url()`, so it adds no font or network request.

## Parent wiring

The phone server allowlist is the only way this file is fetched. Index and the service worker are not enough.

1. Copy the stylesheet to `jarvis/web/phone/checkpoint.css` on the parent tree.
2. In `jarvis/web/phone/index.html`, after `phone.css`: `<link rel="stylesheet" href="/checkpoint.css">`.
3. Add `"/checkpoint.css"` to `SHELL` in `jarvis/web/phone/sw.js`.
4. Add `"/checkpoint.css": "phone/checkpoint.css"` to `SHELL` in `jarvis/phone.py`. `shell_version()` hashes that dict, so the cache name changes with the file.
5. Add the same path to `SHELL` in `tests/mobile/test_phone_web.py`. `test_index_has_no_inline_code_and_only_allowlisted_assets` and `test_service_worker_caches_only_the_shell` compare those lists.

No HTML or JS in the phone app was changed here. No server was started.

## Mock to live selectors

| Approved mock | Live selector | What the sheet does |
|---|---|---|
| Page 16px, title 32/600/−0.035em, 29px under 360px | `body` (already 16px in `phone.css`); `#s-devices .ph h1`, `#s-device .ph h1` | Title lock. At ≤359px the title is 29px. In landscape and height ≤520px it stays 26px, which is the existing short-window size. A 32px rule would have beaten that. |
| Header hairline, Doto wordmark, link dot + ms | `.app[data-screen="devices"] .top`, `.app[data-screen="device"] .top` | Hairline only on those two screens. Wordmark stays 21px from `phone.css` (the mock frame draws 17px). Sparkline stays hidden. |
| Online / Sessions strip, number over the label | `#s-devices .strip`, `dt`, `dd` | 12px radius, 1px `--line`, mono 20px number. `column-reverse` puts the number above the label. DOM order stays label then number. |
| Lede under the title, above the strip | `#s-devices .section-intro` | Type only. The live order is title, strip, lede, list. Not reordered. |
| Rounded device, 16px, surface, 1px rule, min 96px | `#s-devices #dv-list > li`, `#s-devices .dev` | Card chrome moves onto the link. Caps (`.dev-r`) sit under the sentence. Chevron stays `.dev > .i`. |
| Device sentence 15px | `#s-devices .dev-meta` | Geist 15px `--ink-2`. A `data-st="bad"` meta stays red. |
| Manage computers, one quiet link | `#s-devices .manage` | Top rule, min-height 52px. Still one link to `#/fleet`. |
| Control inverted, Camera quiet card, both 16px / min 88px | `#s-device #d-control`, `#s-device #d-camera`, `#s-device .act` | Control is the only inverted action. Camera keeps the page fill and a `--line-ctl` border. |
| Disabled at 50% | `#s-device .act:disabled`, `#s-device #d-control:disabled` | Same look if `aria-disabled="true"` is present. Live buttons use the `disabled` attribute. |
| Pressed quality is not a second primary | `#s-device .seg button[aria-pressed="true"]` | Page fill plus a 1px inset rule. Touch / Trackpad and Settings segments are untouched. |
| Meter cards, no invented percent | `#d-meters .hm-meter`, `.hm-line`, `.hm-v` | 12px radius cards. Label then value, stacked. Value is 16px mono. One column at ≤359px. |
| Computer details | `#s-device .device-details summary` | 52px row. The mock's plus/minus is a stroke chevron, same idea as `.flow-s`, so the stylesheet does not add spoken text. |
| Focus 2px ink, offset 2 | `#s-devices .dev:focus-visible`, `#s-device .act:focus-visible`, and the detail segment, summary, back, manage, session minis | |
| Live dot breathes only when motion is allowed | `.ld[data-st="live"]` inside `prefers-reduced-motion: no-preference` | Overrides the redesign's `animation: none`. `phone.css` still forces `animation: none !important` under reduce. |
| Busy amber ring | `.ld[data-st="busy"]` | Hollow dot, 2px `--needs` border, no blink. |
| Offline hollow dot | `#s-devices .dev .ld[data-st="off"]`, `#s-device #d-dot[data-st="off"]`, `#s-fleet .fl-row .ld[data-st="off"]` | 1.5px `--ink-3`. Not amber. |

## Parity

| Kept | How |
|---|---|
| Routes, ids, three tabs, Devices tab on detail | No HTML change |
| Control before Camera | DOM order. Control is the inverted button |
| Ask target, thread, cost, decision tree, retry, resume | No selector under `#s-ask`, `.flow`, `.request-cost`, or `.ask-target` |
| Settings, theme segment, Forget | No `#s-phone` rules. Quality segment is scoped to `#s-device` |
| Fleet read-only | No `.fl-row` layout rules. Offline fleet dots are locked to the hollow ink dot |
| Sessions, Stop, Touch/Trackpad, camera chrome | No `.sess`, `.stage`, `.dock`, `.end`, or `.key` rules |
| Safe area and keyboard | No change to `--sat`, `--sab`, `--sal`, `--sar`, `data-kb`, or `data-ios-app` |
| Meters | Same nodes from `meters.js`. No bar element and no computed percent |
| Location, fleet admin, MAIN transfer | Not present in this file |

## Checks

Fixture: `/tmp/jarvis-phone-live-checkpoint/fixture.html`. It copies the live class names and a static Devices list plus one device. `phone.css` was copied from the parent tree. `checkpoint.css` was copied from this worktree. Opened with headless Chrome (`/usr/bin/google-chrome-stable` through playwright-core) at `file://`. No server.

Matrix after the final meter rule: 320×800, 390×844, and 844×390, dark and light, Devices and device. 12 renders. Each had `scrollWidth - clientWidth` of 0, and no measured device, manage, back, action, segment, summary, session mini, or tab under 44px.

| Check | Result |
|---|---|
| Body | 16px |
| Title | 29px at 320, 32px at 390, 26px at 844×390. Weight 600 |
| Device card | radius 16px, border 1px, fill `#141414` dark / `#f3f3f3` light |
| Control | `#ededed` on `#000` dark, `#0a0a0a` on `#fff` light, radius 16px, min-height 88px, opacity 1 |
| Disabled camera | opacity 0.5, radius 16px, page fill, not inverted |
| Quality Low | page fill, not the inverted primary. Focus 2px, offset 2px |
| `#d-control` focus | `2px solid rgb(237, 237, 237)`, offset 2px, dark |
| Dot whose text says busy, `data-st="off"` | border `#8a8a8a` dark / `#6b6b6b` light, transparent fill |
| Dot with `data-st="busy"` (fixture only) | border `#f0a640` dark / `#9a5600` light, 2px, transparent, animation none |
| Live dot, motion allowed | animation name `breathe`. `prefers-reduced-motion: reduce` was false in this Chrome |
| Landscape shell | grid `76px 768px`. Tab height 54px |
| Header brand | border box 21×113. Existing `::after` inset `-13px -6px` measures about 47×125 |
| Meter value at 390 | one line, height 21px, card width 168px. Overflow 0 |
| Caps | below the device name. Chevron inside the card |

Screenshots after the final sheet, fallback fonts, 390 wide: `/tmp/jarvis-phone-live-checkpoint/shot-390-devices-icons.png`, `shot-390-device-meters.png`, `shot-390-device-lower.png`. Earlier shots in that directory (`shot-320-*`, `shot-844x390-*`, the first `shot-390-*`) are from before meter values were stacked. The 12-cell overflow and tap check above includes the stacked meters. `document.fonts.check` was false for Geist and for Doto. Root-relative `/fonts/*.woff2` does not load from `file://`. Served `phone.css` still supplies the faces.

Not run: `tests/mobile/web_phone_test.cjs`, `tests/mobile/test_phone_web.py`, the full suite, a served phone, a physical phone, VoiceOver, or the keyboard and safe-area insets. In this Chrome, `env(safe-area-inset-*)` was 0.

## Unsupported

Busy on a real row is still a hollow dot. `phone.js` sets the device, fleet, and detail dots to `live` or `off` (`fillDevRow`, `fillFleetRow`, `renderDetail`). The words "busy, awaiting contact" are text in `.dev-meta` and in `#d-status`. CSS cannot see that text. Painting `data-st="off"` amber would call an offline computer busy. The amber ring is ready for `data-st="busy"` and does nothing until that attribute exists.

The busy sentence is not recolored `--needs`, for the same reason.

`#d-status` stays inside closed `<details class="device-details">`. The user agent hides it until Computer details is opened. The dot beside the title is the visible mark.

Control is not forced off for a busy computer or a down link. The button is disabled only when the script already sets `disabled` (no remote capability and no session of yours). The why text remains `#d-control-why` / `#d-camera-why` inside the button.

These mock pieces are not in the live DOM, so this sheet does not invent them: role chip, written cap names, skeleton rows, meter bars, a status line under the device title, and a second why paragraph outside the button.

The lede stays under the strip. Moving it above the strip would disagree with the reading order.

Ask, Settings, fleet layout, session stage, safe area, and keyboard behavior are unchanged. Location, fleet writes, and MAIN transfer are not added.

Parent integration: appended to existing phone.css, preserving its existing server allowlist and service-worker content hash. No new route or Python restart required.
