# Lan Mouse

*One keyboard and mouse across the laptops, and the rules that keep it from starting while they are apart.*

The build JARVIS runs is the patched tree at `~/src/lan-mouse-jarvis`, binary `~/.local/bin/lan-mouse-jarvis`. On Arch the user unit is `lan-mouse`, left disabled; `lan-mouse-ipwatch.service` is the guard and is what starts and stops it. Do not enable `lan-mouse.service`. On Windows the launcher is `%LOCALAPPDATA%\lan-mouse\start-now.vbs`, started through `explorer.exe`. Elevation uses one-time tasks `JARVIS Lan Mouse` and `JARVIS Lan Mouse Stop`, installed by `jarvis lanmouse elevate` from an administrator terminal, running a copy in `C:\Program Files\JARVIS Lan Mouse`. Never point those tasks at a user-writable path. Re-run `elevate` after a rebuild. `elevate` starts Lan Mouse again only if it was already running. `unelevate` is the explicit task deletion. The guard does not delete or disable the tasks.

The patched build never connects to or accepts a Tailscale address (100.64.0.0/10, `fd7a:115c:a1e0::/48`, or any text that starts with `100.`). `pathsafe.lan_mouse_ip_blocked` is that check. Far apart, an edge crossing finds nobody. wlroots virtual-pointer events need nonzero, increasing timestamps. The patch uses `CLOCK_MONOTONIC`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` is the off switch. The guard never starts it |
| `autostart` | `true` | Once per boot when the machines are together. `false` opts out |
| `off_when_apart` | `true` | The guard stops it while apart. If this is false, the node guard is not launched |

"Together" means the peer is on one of this computer's own subnets. The home LAN is 172.1.x, which is not RFC1918. "Can't tell" changes nothing. `GUARD_STREAK` is 2. The guard stops Lan Mouse after two apart readings and starts it again only if the guard was what stopped it. A manual start is an override until they are together again. A manual stop sticks until the next boot or a verified `jarvis lanmouse start`. The first guard tick does not rearm a stop that was recorded before it. Streaks, settled proximity, and a stale override reset on a recognized boot. A daemon already running at the first tick stays a manual override. A failed stop does not erase the stop intent. A kill from Task Manager before the first tick cannot be told from "not started yet"; use `jarvis lanmouse stop` for a durable stop. Windows boot time is the console logon time, with a 64-bit uptime fallback. If both fail, boot is unknown and per-boot autostart does not run. Times within 120 s count as the same boot.

No automatic start while settled apart, during either remote-control marker, with `enabled` false, or after an explicit stop in this boot. Remote markers normally expire after 60 s. The guard does not unlink them, so it cannot race a live session refresh. Login, not merely power-on, starts the Windows node, through the Startup VBS. The cluster side waits 60 s before the first guard reading.

```text
jarvis lanmouse status | start | stop [--device <id>]
jarvis lanmouse nearby
jarvis lanmouse guard
jarvis lanmouse pair | fingerprint | config | elevate | unelevate
```

`nearby` and `status` include `guard.reason`. `pair --device dell --position right` exchanges fingerprints over the cluster and has each laptop write its own config. The old file is kept as `config.toml.bak-<time>`. Port, release shortcut, and `motion_scale` carry over. Each side must have run Lan Mouse once so it has a certificate. Pairing does not change login autostart. Only the certificate block is hashed. The private key in `lan-mouse.pem` never leaves its laptop. The release chord is Left Ctrl + Left Shift + Left Super (`KeyLeftMeta`) + Left Alt. On Arch, that chord returns control to the Dell. Moving off the Dell's left edge also returns control to Arch. The captured-input fail-safe is the daemon `release_bind`, written next to `authorized_fingerprints`.

Config writes validate the full TOML, the peer, the fingerprint, and nonempty non-Tailscale IPs, then lock, take a unique backup, and replace the file. Unreadable TOML is not overwritten. Explicit pairing still replaces the requested single-peer pairing. The installed watcher must be the repo script, which shares that lock. The old `sed -i` watcher can race a pairing write. Install it only after the matching Python is on that machine, then restart the watcher:

```fish
cp ~/.local/bin/lan-mouse-peer-ips ~/.local/bin/lan-mouse-peer-ips.pre-reboot-reform
install -m 755 scripts/lan-mouse-peer-ips ~/.local/bin/lan-mouse-peer-ips
```

After an apart reading, the watcher skips the peer lookup while `apart_now` is true, which avoids an avahi timeout and a second lookup on every tick. `ExecStartPre` refreshes addresses when the machines are together again.

Remote view pauses Lan Mouse on both ends. See [remote-control.md](remote-control.md). `cluster doctor --fix` re-ensures the guard and does not undo a manual stop.

Cursor stuck on Hyprland:

```fish
hyprctl dispatch 'hl.dsp.cursor.move({ x = 960, y = 540 })'
```

Then restart Lan Mouse on both sides. The Windows UAC secure desktop cannot be reached by any app.

A stuck cursor after reboot is usually one of these, not a deleted task: login never happened, `enabled` or `autostart` is off, the readings are unknown, the launch was accepted but the process exited, two apart readings stopped it and nothing has been together twice since, or a manual stop is still recorded. On the Dell, after login:

```powershell
.venv\Scripts\python.exe -m jarvis lanmouse nearby
.venv\Scripts\python.exe -m jarvis lanmouse status
schtasks /Query /TN "JARVIS Lan Mouse" /V /FO LIST
schtasks /Query /TN "JARVIS Lan Mouse Stop" /V /FO LIST
```

If a task is missing, disabled, or pointed at an old build, David runs `jarvis lanmouse elevate` from an administrator terminal in that checkout. Read `%LOCALAPPDATA%\lan-mouse\daemon.log` if three launch attempts fail. If the process is up and input does not cross, both sides must be the patched Rust build and the fingerprints must match. This recovery path does not change Rust.

## Clipboard, files, and displays

Clipboard and Lan Mouse are separate. `jarvis clipboard start` on each side watches that clipboard and sends new text and images over the cluster, at most 32 MB. Nothing is pushed on connect. A paste of what just arrived does not echo. Secrets marked by a password manager stay put. Files use `jarvis send`, `jarvis fetch`, and `jarvis inbox`, landing in `~/JARVIS-Drop`. The Zenbook's address moves with DHCP. Update `devices.zenbook.target`, or use a hostname or MagicDNS name, when clipboard status says `not linked`. The log is `.jarvis/logs/clipboard.log`.

`dropzone.py` is a thin Wayland strip just inside the Lan Mouse edge. A file dropped on it is copied with `transfer.push`. It accepts COPY only, so the file manager does not delete the original. That is not a shared drag session. The native drag design is [drag-drop-design.md](drag-drop-design.md).

Screen streaming is not paired. Sunshine captures and Moonlight receives. A stream of the existing display is a mirror, not an extra monitor. Windows-as-host would add a virtual display, extend onto it, and capture that output, starting at 1920×1080 at 60 Hz. Arch-as-host can create a headless Hyprland output:

```bash
hyprctl output create headless JARVIS-EXT
hyprctl output remove JARVIS-EXT
```

Creating the output does not create a stream. A headless output may need a portal capture path rather than a DRM output. When Moonlight has the pointer, turn Lan Mouse edge switching off so the same input is not sent twice. An extended Windows desktop still runs those programs on the Dell. It does not merge RAM or GPUs. Do not persist a video or virtual-display setting until it has been tested on these machines.

Acceptance for clipboard: a unique text marker each way, then a screenshot each way (`Win+Shift+S` on the Dell; `grim -g "$(slurp)" - | wl-copy` on the Zenbook). Acceptance for input: Ctrl+C/V, Alt+Tab, Shift, Super, scrolling, and each layout, both directions, plus the release chord.
