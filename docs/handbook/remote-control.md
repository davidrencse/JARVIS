# Remote control

*Driving a computer's screen from the main's console. No new port.*

The Remote view rides the hub. Job kind `remote`. Only the main's console can send it. The brain has no tool for it. The same sessions are what the phone's Control screen uses. However a session ends, the node releases every held key and button.

The node writes `remote-session.json`, pauses its Lan Mouse, and must send a first frame within `START_GRACE` (30 s). The hub keeps the newest frame only. While the session runs, the main's Lan Mouse is paused too (`sharedinput.ControllerPause`, marker `remote-controlling.json`). `ControllerPause.apply` loops until the newest wish is applied, so a quick open then close does not leave Lan Mouse stopped. A marker older than `REMOTE_MARKER_TTL` (60 s) is stale; it is touched every 10 s. Markers from a previous boot are ignored. A future timestamp keeps blocking, because treating it as expired could start Lan Mouse during a live session.

| Constant | Value | Meaning |
|---|---|---|
| `START_GRACE` | 30 s | First frame |
| `MAIN_IDLE` | 20 s | Console silent this long: hub ends the session |
| `NODE_IDLE` | 15 s | Node silent this long after start: hub ends it |
| `CONSOLE_QUIET` | 4 s | Node told to release everything |
| `DEADMAN` | 6 s | No input reply: node lets go of what it holds |
| `MAX_SESSION` | 4 h | Hard cap |
| `KEEP_ENDED` | 120 s | Ended session stays readable so the console can say why |
| `MAX_FRAME` | 2,500,000 bytes | JPEG cap |
| `MAX_EVENTS` / `MAX_QUEUE` | 200 / 2000 | Per request / waiting for the node |
| `MAX_FAILS` | 5 | Failed presses in a row, then the node ends the session |
| Hidden tab | 15 s | `remote.js` ends the session, under `MAIN_IDLE` |

Quality: low 960 px / q45 / 6 fps, medium 1280 / q60 / 10, high 1920 / q75 / 15.

Keys do not reach console shortcuts while a session is starting, live, or stale. Fullscreen uses Keyboard Lock so Ctrl+W goes to the node. Hold Esc to leave. Ctrl+Alt+Shift+Esc releases the keyboard. Closing or reloading the tab asks first. Mouse back and forward are blocked, not forwarded. Wheel steps are clamped to ±20. Horizontal scroll is not sent. A 404 on frames means the main restarted.

A replaced session must not delete the new session's marker or restart Lan Mouse in the middle. The restart is handed on and runs once, after the last session. Second-button and failed-request tracking send releases. Per-session monitor choice on Hyprland includes rotated monitors. SendInput result checks on Windows are not done. Forwarding back and forward buttons is not done.

| You see | Do |
|---|---|
| "<Device> didn't answer within 20 s" | Wake it, press Connect |
| "JARVIS on the main restarted..." | Press Connect |
| "tab was in the background" | Keep the console tab visible |
| "Couldn't move the pointer / click here" | A UAC prompt, locked screen, or admin window is blocking input |
| Lan Mouse stays off | `jarvis lanmouse status`, then `jarvis lanmouse start` if David agrees |

A node crash leaves `remote-session.json`. The next start treats it as dead and cleans up. Tests use fakes: `tests/remote/test_remote_view.py`, `tests/remote/test_remote_lifecycle.py`, `tests/remote/test_remote_web.py`, and `node tests/remote/web_remote_test.cjs`.

`jarvis desktop --device <node> …` is a different path and is cut off at 30 s. Keep those steps short. Stopping control is in [operations.md](operations.md). Lan Mouse rules are in [lan-mouse.md](lan-mouse.md).
