# Final surface repairs (2026-11-11)

Scope: the open items of `AUDIT-CONSOLIDATION-SURFACES.md` that live in the console, the fleet
inventory projection, the phone devices payload and the SSH probe. Correctness repairs only.
Not claimed: lease fencing, an Oracle/MAIN handoff, a canonical C3 read. Nothing was committed,
restarted or enrolled; no live DB, vault or config was touched; no physical iPhone was used.

## What changed

| Area | Repair | Files |
|---|---|---|
| Invite failure | A non-retryable `invite_failed` shows the server's public reason; the false "Trying again reuses the same request" is gone. The uncertain request and node survive label edits (in memory only); Create stays blocked until the operator ticks "I checked. Make a new request for it." No automatic new id. | `jarvis/web/fleet.js` |
| Liveness | One projection, `connections.presence(seen, busy)`: **online** = checked in within 75 s (proof), **busy** = past 75 s but a running job is still inside its deadline (+15 s) = "busy, awaiting contact", **offline** = neither. `online` stays proof-only everywhere. Read-only broker look, no device poll. | `jarvis/connections.py`, `jarvis/fleet_management.py`, `jarvis/phone.py`, `jarvis/web/app.js`, `jarvis/web/fleet.js` |
| Archive | `files_stale`, `archive_baseline_pending`, `archive_baseline_seeded` reach the UI. "Partly verified" is its own state; "Plans, runs and knowledge files are not verified" shows; no "capture complete" wording; git "null ahead" now reads "unknown". | `jarvis/fleet_management.py` (`_ARCHIVE_KEYS`), `jarvis/web/fleet.js` |
| Battery | A Linux SSH probe that saw no `power_supply` (`bats=missing`) no longer replaces a valid CIM battery on a Windows node's card; the node's own reading keeps its source and age. | `jarvis/web/app.js` (`metersForCard`) |
| Connections | Capability chips show titles (`Files and clipboard`), the id stays in the payload. Empty states point to "Connect a computer or server" instead of the CLI invite. | `jarvis/connections.py`, `jarvis/web/app.js` |

### Liveness by surface

- Console Connections (`connections.snapshot`): `presence`, `busy`, `online` (proof).
- Fleet, owner audience: `status` may be `busy`; `online` is proof; `presence` is always set for enrolled nodes.
- Fleet, phone audience and phone devices: `status`/`online` keep the proved word (the phone UI has no busy label yet, so an unknown status would read "Unknown"); `presence` carries the same projection.
- Phone `online` is now the broker's own check-in window; the console's `hub.alive` (which also counts a node that is mid-job) is only the fallback when the broker has no row.
- `link_state()` is unchanged on purpose: the invite and enrolled guards read its old words.

## Tests (new or extended)

- `tests/cluster/test_surface_liveness.py` (8): pure projection; busy seen 100 s is busy in connections, owner fleet and phone fleet and never proved online; genuine offline / expired-deadline job stays offline; online within the window; `link_state` unchanged; reading never creates the broker file; capability titles with ids in the payload; archive allowlist (partial, files_stale, baseline pending, no free text); phone view read-only, no location, no addresses.
- `tests/mobile/test_phone.py::test_overview_busy_node_is_awaiting_contact_not_online_or_offline`.
- `tests/web/web_fleet_test.cjs` (+3, 36 total): uncertain invite after label edit and server reason; busy node; partly verified archive.
- `tests/web/web_app_meters_card_test.cjs` (via `tests/web/test_meters_web.py`): Windows valid battery vs Linux missing probe.
- `tests/runtime/test_hostmeter.py`: probe `bats=missing` is "unavailable" and labelled SSH status.
- `tests/web/fleet_fixtures.json`: stale cloud sentence updated to the server's text.

## Remaining limits

- Not mine, still to change: `jarvis/web/phone/phone.js` (add a `busy` label, read `d.presence`); `jarvis/settings.py:230`, `jarvis/ui.py:415,1231` still use their own online test; `jarvis/web/settings.js:181` and `jarvis/web/index.html:374` still say to run `jarvis cluster invite`.
- The uncertain-invite memory is in the page only (the static test forbids web storage). A reload forgets it; the server still refuses the same request id with `invite_failed`, so nothing is silently re-issued.
- "Busy" depends on the broker's job deadline; a hung job stops counting after deadline + 15 s.
- The overview CIM battery is preferred only when the probe's battery is `unavailable`; a Linux probe that reads a battery is used as before.
- Browser checks were Node DOM + fake browser only, no real browser or phone.

## Safe activation

1. Python changed: restart the JARVIS process on the main (David does this).
2. `jarvis/web/` files are live on reload.
3. Nodes run the same code: `jarvis cluster update <node>` for each online node; an offline node is left alone.
4. Check: Devices shows "busy, awaiting contact" only while a job runs past 75 s of silence; Fleet shows a partly verified archive as "Partly verified".

## Parent integration follow-up

Completed the remaining presence consumers: phone Devices, device detail and Manage computers now label busy explicitly; Settings exposes the shared presence; console hardware cards and Remote use the same projection. Remote reports a stale busy node as awaiting contact and does not advertise it as online or ready. Replaced the remaining Settings/index CLI invite guidance with Connections onboarding.

Added a remote busy-node regression and updated release-readiness fixtures to provide actual check-in timestamps. Focused integration: 38 tests passed (`ReleaseReadinessTests`, `DevicesTests`, surface liveness, settings and phone web). `git diff --check` passed. These results supersede the first remaining-limit bullet above. No restart or live input was performed.
