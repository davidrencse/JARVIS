# Operations runbook

Python changes need the process restarted. `jarvis/web/` files are live on a console
reload. Say which one applies when you report.

## What runs on the Arch main

User units seen in `~/.config/systemd/user`: `jarvis-voice`, `jarvis-cluster`,
`jarvis-clipboard`, `jarvis-knowledge-check` (timer), `lan-mouse` (disabled, see below),
`lan-mouse-ipwatch` (the apart guard). The console is `jarvis ui` on `:7777`.

## Restarts (David does these; Claude asks first)

| Changed | Restart | Command (fish) |
|---|---|---|
| `daemon.py`, `listen.py` | voice listener | `systemctl --user restart jarvis-voice` |
| `cluster.py`, `remote_view.py`, `clipsync.py`, hub code | cluster hub | `systemctl --user restart jarvis-cluster` |
| `ui.py` and other console Python | `jarvis ui` | restart the console process |
| `jarvis/web/*.js, css` | nothing | reload the page |
| any code a node runs | node | `jarvis cluster update <node>` |

`lan-mouse.service` stays disabled; the guard starts and stops it. Do not enable it.

## Cluster update

```sh
jarvis cluster update <node>             # push current code
jarvis cluster update <node> --rollback  # swap back to the previous code
jarvis cluster doctor                    # diagnose
```

Offline node: say so, leave it, do not retry. Dell: only when David says it is available.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests          # about 2.5 min; all must pass
.venv/bin/python -m unittest tests.test_parse           # one module
.venv/bin/python -m unittest tests.test_web_voice       # after touching app.js or voice.js
node tests/web_remote_test.cjs                          # remote.js scenarios
```

The suite never spends quota: an audit hook refuses to start `codex` or `claude` under
tests or with `JARVIS_NO_MODELS=1`. `jarvis eval` is free; `jarvis eval --brain` spends
subscription quota, so ask first.

## Fail-safes

| Problem | Fix |
|---|---|
| JARVIS is driving the mouse | SUPER+SHIFT+ESCAPE, or `jarvis control stop`, or say "Jarvis, stop controlling my computer". It also pauses when you move the mouse |
| Cursor stuck (Hyprland) | `hyprctl dispatch 'hl.dsp.cursor.move({ x = 960, y = 540 })'`, then restart Lan Mouse both sides |
| Remote view will not let go | Ctrl+Alt+Shift+Esc in the console, or Disconnect; the node's `jarvis control stop` also ends it |
| Lan Mouse off and should be on | `jarvis lanmouse nearby`, then `jarvis lanmouse start` |
| Voice ignores David | Check `speaker_check`, run `jarvis voice test`; `jarvis voice recalibrate` rebuilds the voiceprint |
| Clipboard not syncing | `systemctl --user status jarvis-clipboard`; a hand stop holds until next boot |

Never make a system-wide cursor or input change without a tested way back.

## Troubleshooting

- **A node refuses jobs**: code mismatch. `jarvis cluster update <node>`.
- **Remote desktop commands** (`jarvis desktop --device <node> terminal`) are cut off at
  30 s. Keep steps short.
- **`pkill -f <pattern>`** can match your own shell. Use a pidfile or `pgrep -x`.
- **Windows**: daemons die with the command shell that launched them; start through
  `explorer.exe`. New AppData folders created under Claude's shells are hidden from Windows.
- **Terminal safety**: on Linux every brain command runs in bubblewrap
  (`terminal.sandbox`; off: `terminal.sandbox: false`). Under `rules.confirm: never`,
  money and messages still wait for David (`rules.always_ask`).
- Brain commands run for at most 30 s (`terminal.TIMEOUT`).
