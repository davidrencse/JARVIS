# Remote control

David drives a node's screen, mouse and keyboard from the main's console (the Remote
view). It rides the existing hub and tunnel: no new port. Job kind `remote`; only the main's
console can send it, and the brain has no tool for it.

**Invariant:** however a session ends, the node lets go of every held key and button.

## Lifecycle

```mermaid
sequenceDiagram
    participant C as Console (remote.js)
    participant H as Hub on main
    participant N as Node (remote_view.py)
    C->>H: /api/remote/start
    H->>H: Sessions.open(pending=True)
    H->>N: job: start session
    N->>N: write remote-session.json, pause its Lan Mouse
    N->>H: first frame (within 30 s)
    H->>H: Sessions.touch, state live
    loop while live
        N->>H: /v1/remote/frame (JPEG)
        H-->>C: newest frame only
        C->>H: /api/remote/input (events)
        H-->>N: input poll reply
        N->>N: inject; dead-man timer reset
    end
    alt Disconnect / stop job / closed tab / link loss / restart
        C->>H: /api/remote/stop
        H-->>N: end
        N->>N: release held keys and buttons
        N->>N: delete own marker, restart Lan Mouse once
    end
```

While a session runs, the main's Lan Mouse is paused too (`sharedinput.ControllerPause`,
marker `remote-controlling.json`) so David's mouse and keyboard stay on the main.

## Limits (`remote_view.py`)

| Constant | Value | Meaning |
|---|---|---|
| `START_GRACE` | 30 s | Node has this long to send its first frame |
| `MAIN_IDLE` | 20 s | Console silent this long: hub ends the session |
| `NODE_IDLE` | 15 s | Node silent this long after starting: hub ends it |
| `CONSOLE_QUIET` | 4 s | Total console silence: node told to release everything |
| `DEADMAN` | 6 s | No input reply: node lets go of what it holds |
| `MAX_SESSION` | 4 h | Hard cap |
| `KEEP_ENDED` | 120 s | Ended session stays readable so the console can say why |
| `MAX_FRAME` | 2,500,000 B | JPEG size cap |
| `MAX_EVENTS` / `MAX_QUEUE` | 200 / 2000 | Per console request / waiting for node |
| `MAX_FAILS` | 5 | Failed presses in a row before the node ends the session |
| hidden tab | 15 s | Console (`remote.js`) ends the session; stays under `MAIN_IDLE` |
| `REMOTE_MARKER_TTL` | 60 s | Marker older than this is stale (touched every 10 s) |

Quality presets: low 960 px / q45 / 6 fps, medium 1280 / q60 / 10, high 1920 / q75 / 15.

## Console behaviour

- Keys never reach console shortcuts while a session runs (including Starting and stale).
- Fullscreen uses Keyboard Lock so Ctrl+W and similar go to the node; hold Esc to leave.
  Ctrl+Alt+Shift+Esc releases the keyboard.
- Closing or reloading the tab mid-session asks first.
- Mouse back and forward are blocked, not forwarded. Wheel steps are clamped to +/-20.
  Horizontal scroll is not sent.
- A 404 on frames means the main restarted; the console says so and asks you to Connect again.

## Troubleshooting

| You see | Do |
|---|---|
| "<Device> didn't answer within 20 s" | Check it is awake, press Connect again |
| "JARVIS on the main restarted..." | Press Connect |
| "tab was in the background" | Keep the console tab visible |
| "Couldn't move the pointer / click here" | On Windows, a UAC prompt, locked screen or admin window blocks input; close it |
| Lan Mouse stays off after a session | `jarvis lanmouse status`; the guard or next boot brings it back, or `jarvis lanmouse start` (ask David first) |

A node crash leaves `remote-session.json` in its state folder; the next start finds it,
treats the session as dead and cleans up.

Tests use fakes only: `tests/test_remote_view.py`, `tests/test_remote_lifecycle.py`,
`tests/test_remote_web.py`, and `node tests/web_remote_test.cjs`.
