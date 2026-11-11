# What changed in mobile targeting and native app routing

These are changes from the phone-to-Dell/native-app task, separate from the earlier PWA build.

| Change | Implementation | Evidence and limits |
| --- | --- | --- |
| Choose a PC in mobile Ask | `jarvis/web/phone/index.html` adds **Send to**. `phone.js` populates it from the device list, remembers selection, sends `device`, and preserves it for retry/resume. | Real browser fixture checked the request payload at 390, 320 and landscape widths. That fixture mocked hardware responses. |
| Execute on the selected PC | `jarvis/phone.py` validates the device and node capabilities, then submits with `origin_device=target`. `jarvis/ui.py` carries it into the worker context. Existing remote tools use that context for their default device. | Phone HTTP tests cover Dell target, unavailable/unknown devices and retry-key separation. A separate real read-only `list_apps` call to Dell succeeded. This was not a physical-phone-to-Dell end-to-end test. |
| Keep approvals on main | `topology.approvals_on()` carries a separate approval origin. The worker sets it, and the pipeline/assistant use it when parking approval requests. | Pipeline regression verifies a command targeting Dell parks approval on main. Mobile approval-word refusal remains in place. |
| Stop requiring Chrome for Spotify | `jarvis/actions.py` tool instructions now prefer native Spotify search, then accessibility or screenshot/keyboard/mouse selection and playback verification. The invalid-target guidance also points to that flow. | This is an instruction/routing change, not a new Spotify playback engine. No live exact-song playback benchmark was performed. Browser fallback remains possible when native controls fail. |
| Prefer native desktop operation | The same instructions prefer `open_app`, `app_ui` and existing target-specific `computer` tools, including checking actual keybinds. | Hyprland and Windows keyboard/mouse backends already existed. This task did not implement new backends or prove an overall latency improvement. |

The main services were restarted after the changes, and the phone server was confirmed listening.
A Claude delegate for the native-app investigation failed due to a provider rate limit; the parent
implemented the changes directly.

## Test organization follow-up

Tests now live in subsystem folders documented in `tests/README.md`. Imports, fixture paths,
JavaScript source paths and runnable documentation commands were updated. Discovery before the
move found an existing indentation error in `test_clipsync.py`; correcting that import indentation
recovered 48 tests. After the move, discovery finds 2,950 tests with zero import errors and no loss
of any previously importable test ID. Full execution is checked separately from discovery.

Verification notes for the reorganization: compilation, mobile JavaScript scenarios,
remote JavaScript scenarios, the 15 standard voice-browser Python tests and 19 agent tests
passed. Running *all* standalone voice JavaScript scenarios additionally found a legacy
hands-free expectation (`/api/listen?wake=1`) that fails against current app.js; the original
unmoved fixture reproduces the same failure. Its assertions were left intact. The agent
JavaScript harness requires a named scenario, so `tests/README.md` uses its Python wrapper.
