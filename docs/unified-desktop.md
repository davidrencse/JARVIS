# One workspace across Windows and Arch

The goal is shared copy/paste, shared keyboard/mouse, remote viewing/control,
and optional display extension. These are planned integrations; none is claimed
paired or validated on both laptops yet.

| Capability | Initial candidate | What still needs testing |
|---|---|---|
| Shared clipboard | `jarvis clipboard` over SSH (built) | Dell side on real Windows apps; Zenbook side verified |
| Shared mouse/keyboard | Lan Mouse | Edge crossing, modifiers, layouts, reconnects and release shortcut |
| Screen stream/control | Sunshine host + Moonlight client | Encoder, capture backend, latency and input behavior |
| Dell desktop extended onto ASUS | Windows virtual display + Sunshine, Moonlight fullscreen on Arch | Independent second display, placement and resolution |
| ASUS desktop extended onto Dell | Hyprland headless output + compatible capture, Windows receiver | Actual installed Hyprland version and virtual-output capture |

## Clipboard

`jarvis clipboard` is built in: copy on one laptop, paste on the other. Text and
images (PNG, plus a bitmap on Windows so Paint and Office paste it), both ways.
It reuses the Dell → Zenbook SSH link from `jarvis send`: same key, BatchMode and
strict host-key checking. No new port, pairing code, crypto or app on either side.

The Dell hosts it. `jarvis clipboard start` opens one SSH session to the Zenbook and
sends `jarvis/clipsync.py` itself as the remote half (stdlib Python + `wl-clipboard`,
finds the Hyprland session even from SSH). One JSON line per copy goes each way.
It reconnects with backoff after sleep, Wi-Fi changes or a reboot.

```powershell
jarvis clipboard start            # on the Dell
jarvis clipboard status           # running / linked / recent log lines
jarvis clipboard autostart on     # start hidden at Windows login
jarvis clipboard stop
```

- Nothing is pushed on connect; only new copies sync. Pasting what came from the
  other side doesn't echo back.
- Copies a password manager marks secret stay put: KeePassXC/KDE's
  `x-kde-passwordManagerHint: secret` on Linux, and Windows'
  `ExcludeClipboardContentFromMonitorProcessing`.
- Files aren't synced. Use `jarvis send` or drop them on the console.
- The Zenbook's IP changes with DHCP. Update `devices.zenbook.target` (or use a
  hostname or Tailscale name) if `status` shows `not linked`.
- Log: `.jarvis/logs/clipboard.log`. Up to 32 MB per copy.

Acceptance: copy a unique text marker each way, then a screenshot each way
(Win+Shift+S on the Dell; `grim -g "$(slurp)" - | wl-copy` on the Zenbook).

## Input

[Lan Mouse](https://github.com/feschber/lan-mouse) lists Windows and Hyprland
support. Pair the two devices, choose which screen edge faces the other laptop,
and verify the release binding before using it full time. Arch installation is
documented as `sudo pacman -S lan-mouse`; Windows releases are available upstream.

Test Ctrl+C/V, Alt+Tab, Shift, Super/Windows, scrolling, and each keyboard layout
in both directions. Upstream documents modifier limitations on some Wayland
receiver backends and a Windows cursor caveat when no physical mouse is attached.
Therefore “works on Hyprland” remains a testable claim for the installed versions.
Clipboard sharing remains a separate integration.

## Video and extended displays

[Sunshine](https://docs.lizardbyte.dev/projects/sunshine/latest/) captures and
encodes a desktop; [Moonlight](https://moonlight-stream.org/) receives it on
Windows, Linux or iOS. Pair with the host before exposing a stream. A stream of
the existing display is mirroring/control, not an extended display.

For **Windows as host**, the [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)
creates another Windows monitor. Configure Windows to extend onto it, tell
Sunshine to capture that output, then open the stream fullscreen in Moonlight
on the ASUS. This is a documented component combination, not yet a tested setup
on this Dell. Begin with 1920×1080 at 60 Hz and measure usability before raising
resolution or refresh rate. Driver installation is an OS-level setup step.

For **Arch as host**, Hyprland documents virtual headless outputs:

```bash
hyprctl output create headless JARVIS-EXT
# After inspecting the actual output with hyprctl monitors all:
hyprctl output remove JARVIS-EXT
```

Creating an output alone does not make a stream. A headless compositor output
may require a portal/Wayland capture route rather than capture of a physical DRM
output. Select the backend only after checking the installed Hyprland and
Sunshine versions and testing a moving window on that virtual display.
See [Hyprland's output commands](https://wiki.hypr.land/0.50.0/Configuring/Using-hyprctl/).

Test native-app windows moving onto the additional display, independent content
on both screens, pointer alignment and recovery when the receiver disconnects.
Avoid two simultaneous input-forwarding paths: when Moonlight handles control,
disable edge switching that would send the same input through Lan Mouse.

An extended Windows desktop still runs Windows applications on the Dell. It does
not migrate those processes into Arch or combine the machines' RAM/GPUs.

## Connection status and order

1. Verify the ASUS SSH host key, then authenticate and collect its specs.
2. Establish a working local network route and pair clipboard/input apps.
3. Test video on an existing display, then add the virtual output.
4. Persist only tested settings and expose their status through JARVIS.

Tailscale is installed on the Dell, but its service pipe denied access from the
agent session. Its login and peer state remain unknown. Tailscale supplies
connectivity; it does not itself supply SSH, clipboard or desktop services.
See [Tailscale device connectivity](https://tailscale.com/kb/1452/connect-to-devices).
