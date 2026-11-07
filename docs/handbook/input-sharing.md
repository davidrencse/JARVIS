# Input sharing (Lan Mouse)

One mouse and keyboard across the two laptops, using a JARVIS-patched Lan Mouse.

| Item | Where |
|---|---|
| Source | `~/src/lan-mouse-jarvis` |
| Binary | `~/.local/bin/lan-mouse-jarvis` |
| Service (Arch) | user unit `lan-mouse`; guard in `lan-mouse-ipwatch.service` |
| Windows | `%LOCALAPPDATA%\lan-mouse\start-now.vbs`, started via `explorer.exe` |

## Rules baked in

- **Same network only.** The patched build never connects to or accepts a Tailscale
  address (100.64.0.0/10, fd7a:115c:a1e0::/48). Far apart, an edge crossing finds no one.
- **Autostart per boot** when both PCs read together; off while apart.
  `lan_mouse.autostart: false` opts out. A manual stop sticks until the next boot.
- **Off while apart.** `jarvis lanmouse guard` stops Lan Mouse after `GUARD_STREAK = 2`
  readings in a row show the peer elsewhere. "Together" means the peer is on one of this
  computer's own subnets (the home LAN is 172.1.x, not RFC1918). "Can't tell" changes
  nothing. It restarts Lan Mouse only if the guard stopped it; a manual start counts as an
  override.
- **Remote view pauses it** on both ends (see [remote-control.md](remote-control.md)).
- **Windows elevation**: on-demand tasks `JARVIS Lan Mouse` and `JARVIS Lan Mouse Stop`
  (one-time `jarvis lanmouse elevate` from an admin terminal) run a copy in
  `C:\Program Files\JARVIS Lan Mouse`. Re-run `elevate` after rebuilding. Never point the
  tasks at a user-writable path. `elevate` now starts Lan Mouse again only if it was
  running before.
- wlroots virtual-pointer events need nonzero, increasing timestamps; the patch uses
  `CLOCK_MONOTONIC`.

## Config (`lan_mouse`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` is the off switch; the guard never starts it |
| `autostart` | `true` | Guard brings it up once per boot when together |
| `off_when_apart` | `true` | Guard stops it while apart |

## Commands

```sh
jarvis lanmouse status | start | stop [--device <id>]
jarvis lanmouse nearby        # current together/apart reading
jarvis lanmouse guard         # one guard pass
jarvis lanmouse pair | fingerprint | config | elevate | unelevate
```

## Cursor stuck (Hyprland)

```fish
hyprctl dispatch 'hl.dsp.cursor.move({ x = 960, y = 540 })'
```

Then restart Lan Mouse on both sides. The Windows UAC secure desktop can never be reached
by any app; that is a Windows limit.

## Known open issue

`ControllerPause.set` in `sharedinput.py` can leave the main's Lan Mouse stopped after a
very quick open then close of a Remote session. Fix: loop in `apply()` until the wanted
state equals what was applied. Not done in this overhaul (see [audit-2026-11.md](audit-2026-11.md)).
