# Operations

*Restarts, sleep, machine readings, and the checks that are still worth running.*

Python changes need that process restarted. `jarvis/web/` reloads with the page. Say which one applies. Node code is [release.md](release.md).

## Units on the Arch main

User units in `~/.config/systemd/user`: `jarvis-voice`, `jarvis-cluster`, `jarvis-clipboard`, `jarvis-knowledge-check` (timer), `lan-mouse` (disabled; the guard owns it), `lan-mouse-ipwatch`. The console is `jarvis ui` on `:7777`.

| Changed | Restart |
|---|---|
| `daemon.py`, `listen.py` | `systemctl --user restart jarvis-voice` |
| `cluster.py`, `remote_view.py`, `clipsync.py`, hub code | `systemctl --user restart jarvis-cluster` |
| `ui.py` and other console Python | the `jarvis ui` process |
| `jarvis/web/*` | reload the page |
| code a node runs | automatic after a green push to origin/main; `jarvis cluster update <node>` only speeds it up |

David runs restarts. Do not enable `lan-mouse.service`.

## Sleep

```fish
.venv/bin/python -m jarvis sleep
.venv/bin/python -m jarvis sleep --no-suspend
.venv/bin/python -m jarvis wake
```

`sleep` stops the voice listener, hub, clipboard, and Lan Mouse, IP watcher first, and any user service holding a hard sleep lock. A transient `jarvis-wake-*` unit starts exactly those after wake, even if the terminal is closed. If logind refuses, everything comes straight back. Nodes stay up and retry every 15 s. The Dell's guard turns its Lan Mouse off while the main is gone. Closing the lid suspends without this pause, because logind ignores app locks for the lid, and JARVIS is frozen rather than paused.

## Tests

```sh
env $(env | grep -o '^CODEX_[A-Z_]*' | sed 's/^/-u /') JARVIS_NO_MODELS=1 .venv/bin/python -m unittest discover -s tests
.venv/bin/python -m unittest tests.intelligence.test_parse
.venv/bin/python -m unittest tests.web.test_web_voice
node tests/remote/web_remote_test.cjs
.venv/bin/python -m jarvis eval
```

The suite does not spend quota. `jarvis eval --brain` does, so ask first. `tests.web.test_web_voice` is the microphone-startup regression: a failed AudioWorklet load must release tracks and the audio context.

The supported run is a source checkout. An isolated wheel omits `jarvis.config.json`, so `python -m jarvis doctor` from that wheel exits on a missing file, and it omits `.claude/agents`. The `voice` extra lists the recorder, Whisper, and speaker verification, and omits `kokoro-onnx`. Install Kokoro from the command in [reference.md](../reference.md). Live audio, overlay focus, Lan Mouse, and cross-machine copy stay on the real machines, outside the offline suite.

## Fail-safes

| Problem | Fix |
|---|---|
| JARVIS is driving the mouse | Super+Shift+Escape, or `jarvis control stop`, or "Jarvis, stop controlling my computer". Moving the mouse also pauses it. At most 120 actions per 10 minutes. An error releases buttons and modifiers. `jarvis control stop` refuses further actions for 10 minutes |
| Cursor stuck on Hyprland | `hyprctl dispatch 'hl.dsp.cursor.move({ x = 960, y = 540 })'`, then restart Lan Mouse both sides. See [lan-mouse.md](lan-mouse.md) |
| Remote view will not let go | Ctrl+Alt+Shift+Esc, or Disconnect. The node's `jarvis control stop` also ends it |
| Lan Mouse off and should be on | `jarvis lanmouse nearby`, then `jarvis lanmouse start` |
| Voice ignores David | `speaker_check`, `jarvis voice test`, `jarvis voice recalibrate` |
| Clipboard not syncing | `systemctl --user status jarvis-clipboard`. A hand stop holds until the next boot |
| A node refuses jobs | Code mismatch. [release.md](release.md) |

`pkill -f <pattern>` can match the shell that ran it. Use a pidfile or `pgrep -x`. On Windows, a daemon started from a shell dies with that shell; start it through `explorer.exe`. A folder created under AppData from Claude's shell can be hidden from Explorer. On Linux every brain command runs in bubblewrap unless `terminal.sandbox` is false. Under `rules.confirm: never`, money and messages still wait (`rules.always_ask`). Brain commands run at most 30 s.

## Machine readings

The console Machines tab, the Connections card, and the phone's device screen show the same figures. The page prints what the backend computed. A missing field stays missing. There is no notification and no automatic action.

Battery charge is how full it is now, plus charging, discharging, full, or AC. Health is full-charge capacity divided by design capacity, and only when both numbers exist in the same unit. Energy is never divided by charge. Nothing estimates health from the current percent, uptime, or voltage. No design capacity means Unavailable and why. A machine with no battery is Not applicable. An old inventory with no battery section is Not reported. Linux reads `/sys/class/power_supply`, bounded and read-only. Windows asks Win32_Battery and, when both sides report a capacity, `root\wmi` BatteryStaticData and BatteryFullChargedCapacity. If those two ratios disagree by more than 5 percent, neither is chosen. The script does not write a battery report, does not use `powercfg`, and does not elevate. Cycle count is shown only when reported. A health figure above 100 percent is shown as "of design" and is not clamped. Above 150 percent it is still shown, with "check units". Degradation is the gap from that same rounded percent. A charge outside 0–100 is Unavailable, with the raw number beside it. Two batteries stay two batteries. The pack line is the design-weighted sum, and only when every battery reported health in the same unit.

RAM, swap, and disk are Used, Free, and Total. Free plus Used equals Total. GB is 1024³, trailing zeros removed. Linux RAM free is MemAvailable. Windows RAM free is FreePhysicalMemory. The detail line says which. Swap free is unused swap. No swap, or a zero-size page file, is Not applicable and the group is still shown. Free larger than total is Unavailable, not clamped. Disk free is the space this account can use. Root-reserved blocks sit in Used. The total is the filesystem that was measured, not the whole drive. Letters are kept (`C:`). Volume labels, home paths, and usernames are not. CPU on Linux is the 1-minute load average divided by the logical CPU count, not a busy percent, and it is not clamped at 100. If the core count is missing, the load average is shown on its own. CPU on Windows is Win32_Processor LoadPercentage. The two are labelled differently.

Fresh is 30 minutes or less, aging is 6 hours or less, and stale is older. No timestamp is "time unknown". A timestamp more than two minutes in the future is "time doesn't agree". An offline machine keeps its last numbers and says they are the last reading. Inventory is not collected on a timer. It updates when someone runs `jarvis inventory`, or when a node running this code puts a newer reading on the heartbeat. The heartbeat is cached about 20 s on the node. The first Windows read can take the CIM timeout (8 s) and is a separate query, so a hang there does not blank the hardware list.

No serial number, MAC, product key, Windows UniqueID, or battery DeviceID is shown. A battery is "BAT0" or "Battery 1".

```text
jarvis inventory
jarvis inventory --device ID --output .jarvis/inventory/ID.json
```

Probed 2026-10-20, with no serials kept. Live SSH targets are in `jarvis.config.json` and move with DHCP. The ASUS host-key fingerprint checked on the machine is `SHA256:Up97wSPjhl95DuIoI6bTvYd53OZDfAZ92jnpEQ53QPM`.

| | Dell G15 5530, Windows 11 | ASUS Zenbook Pro 15 Flip OLED, Arch |
|---|---|---|
| Hostname | WinLaptop | archmain |
| CPU | i7-13650HX, 14 cores (6P+8E) / 20 threads | i7-12700H, 14 cores (6P+8E) / 20 threads, max 4.7 GHz, 24 MiB L3 |
| GPU | RTX 4060 Laptop, 8188 MiB (`nvidia-smi`; AdapterRAM is capped at 4 GiB and is not the VRAM figure), plus Intel UHD | Arc A370M, plus Iris Xe |
| RAM | 16 GiB, 2×8 GiB @ 4800 MT/s. Usable is lower because firmware reserves some | 16 GB; kernel sees 15.3 GiB |
| Swap | Windows pagefile | 7.7 GiB zram plus disk swap (23.7 GiB total) |
| SSD | SK hynix BC901 512 GB, plus a 1 TB USB SSD | SK hynix 1 TB. The ext4 root is about 310 GiB. The rest of the drive is a BitLocker Windows install, not a 304 GiB disk |
| Panel | 1920×1080 @ 165 Hz, 15.6-inch class (34×19 cm) | 2880×1620 @ 120 Hz, same 15.6-inch class. EDID is 340×190 mm |
| OS | Windows 11 Home | Arch, Hyprland |

The Dell battery was not measured in that probe. Linux health on the main can be computed from an inventory that already stored `energy_full` and `energy_full_design`. A node's live charge, Windows capacities, and RAM, swap, disk, and CPU appear after that node runs this code.

Restart the main so the console and the hub pick up meter Python. Reload the page for script and CSS. The phone picks up `/meters.js` on its next load.
