# Phone UI usability review

Reviewed 2026-11-09 during the mobile interface redesign. This review exercises the shipped
HTML, CSS and JavaScript in real headless Chromium with mobile viewport and touch settings.
Only the network and hardware boundary is simulated: no real PC screen, camera, location or
JARVIS task is activated, and production authentication is unchanged.

## Coverage and observed results

The isolated browser fixture exercised these flows:

- Devices: open the list, select Dell, reach its screen, camera and location actions.
- Control: start a simulated screen, switch to Trackpad, stop; inspect 320 × 568 portrait
  and 667 × 375 landscape with the keyboard controls visible.
- Camera: start a simulated feed, see the live picture, stop.
- Locations: request a position, show its result and Maps action, then show an actionable
  Windows location error; open the Locations tab.
- Ask: type and send a question, read the simulated reply.
- Settings: inspect phone identity/session controls and switch to light theme.
- Pairing: display the unpaired form; with an iPhone user agent, display Home Screen
  installation instructions and hide the browser pairing form.

The final browser pass captured 15 states at 375 × 667, 390 × 844, 320 × 568 and
667 × 375. It observed no JavaScript page errors or horizontal document overflow. Both
light and dark themes were inspected. Screen-control mode and theme selections responded.
The landscape gesture help could be scrolled fully into view.

## Findings resolved

The initial source-order regression failed because Control, Camera and Locate followed the
computer diagnostics and quality settings. Actions now precede quality and collapsed computer
details. At 375 × 667, Control moved from approximately y=533 to y=173.

Location feedback and its Maps action now appear directly after the actions. The short
portrait location radar is smaller, so the list needs less scrolling. Landscape control help
has room at the bottom of the scrolling dock. Location-row buttons now have 44 px visible
height. The logo retains an expanded touch target.

## Commands observed

```sh
node tests/mobile/web_phone_test.cjs
node /tmp/jarvis-mobile-usability.cjs
JARVIS_TEST_IOS=1 node /tmp/jarvis-mobile-usability.cjs
```

The existing VM behavior suite produced 24 `PASS` lines and exited 0. The isolated browser
fixture exited 0 on the final run, reporting the 15 states above, no page errors, no document
overflow, and passing checks for the primary action appearing above the fold and landscape
help scrolling into view. The iPhone-user-agent pass exited 0 and verified the install gate.
Temporary fixture, screenshots and JSON diagnostics are under `/tmp/jarvis-mobile-usability*`
and `/tmp/jarvis-usability-*.png`; they are session artifacts, not release dependencies.

## Limits

Chromium mobile emulation is not a physical iPhone or Safari. Real iOS installation,
software-keyboard behavior, VoiceOver, Tailscale pairing, Windows permissions, camera hardware,
remote input delivery and actual geolocation still require a phone and connected computers.
The behavioral VM suite covers pairing approval, network recovery, input gestures, background
session cleanup and pending Ask approvals; those are simulated behavioral checks rather than
claims of successful hardware use. This review does not certify the previously blocked Dell
code deployment.
