# Approved UI live integration — 2026-11-11

Applied the approved checkpoint to existing live markup, preserving routes, IDs, controls and backend contracts. The phone uses an appended section in `jarvis/web/phone/phone.css`; the console loads `jarvis/web/checkpoint.css` last from `index.html`. The existing server reads both from disk; no service restart or data migration occurred.

## Evidence

- Live GET `http://127.0.0.1:7787/phone.css`: 200, approved section present.
- Live GET `http://127.0.0.1:7777/`: 200, checkpoint link present.
- Live GET `/checkpoint.css`: 200, new stylesheet present.
- Focused Python frontend set: 42 tests OK. Earlier phone shell/e2e: 18 tests OK.
- Node DOM: fleet 36, phone 42, remote 17, meters 16 scenarios passed.
- Live console browser at 1280×800, 1180×820, 720×450, 320×800: no document horizontal overflow; prompt and gate present.
- Console screenshot: `/home/jdoe/.t3/userdata/browser-artifacts/browser-screenshot-127-0-0-1-mwcp6skn-2648f987.png`.
- Phone fixture screenshots: `/tmp/jarvis-phone-live-checkpoint/shot-390-devices.png`, `shot-390-device.png`. These are fixture evidence, not a paired physical phone.
- Full suite log: `/tmp/jarvis-live-ui-checkpoint-full.log` (result recorded after completion).

## Limits and open issues

The pre-existing standalone voice DOM `always_on_sends_only_requests_that_start_with_jarvis` fails: it expects wake=1 while driving the Space push-to-talk path. Python wrapper omits that scenario; no voice implementation changed in this visual change. Physical PWA safe areas/keyboard, screen readers, real camera/input, and live node operations were not tested. No latency or performance improvement claimed. Console 3D stage remains present to preserve functionality; the mock's simplified center is not a removal authorization.

Live integration remains uncommitted on `implementation/ui-parity-checkpoint-20261111` amid pre-existing dirty changes. The separate mock alone was pushed as c2f0455 on `t3/ui-design-checkpoint`. Existing files backed up as `.pre-approved-checkpoint` before editing.

## Integration correction

The first full run executed 3577 tests in 280.277s with one Remote layout failure and one archive test error. At 320px the expanded header covered the Remote image. The skin now uses the compact Remote header at widths <=640px, keeping theme/navigation available and hiding header extras while Remote is open. All 7 rendered Remote checks then passed. The archive test passed in the focused rerun after its existing fixture decoded the string-valued spec; no archive production code was changed here. Final full log: `/tmp/jarvis-live-ui-final-suite.log`.

Final full discovery: 3567 tests in 280.987s, OK (7 skipped, 1 expected failure). Log `/tmp/jarvis-live-ui-final-suite.log`. The independently run standalone voice DOM scenario still fails as documented; it is not covered by discovery.
