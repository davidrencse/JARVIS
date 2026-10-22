# Hardware inventory

Recorded October 20, 2026. Both columns come from live probes: Dell from local
PowerShell CIM, storage and `nvidia-smi` queries; ASUS from the JARVIS collector
over verified SSH (`jdoe@172.1.42.215`, host key
`SHA256:Up97wSPjhl95DuIoI6bTvYd53OZDfAZ92jnpEQ53QPM`). Raw snapshots are in
`.jarvis/inventory/`. No serial numbers or service tags are retained.

| Component | Dell G15 5530 — Windows 11 | ASUS Zenbook Pro 15 Flip OLED — Arch |
|---|---|---|
| Model string | Dell G15 5530 | ASUSTeK Zenbook UP6502ZD_UP6502ZD |
| Hostname | WinLaptop | archmain |
| CPU | Intel Core i7-13650HX | Intel Core i7-12700H |
| Cores / threads | 14 (6P + 8E) / 20 | 14 (6P + 8E) / 20, max 4.7 GHz, 24 MiB L3 |
| Discrete GPU | NVIDIA GeForce RTX 4060 Laptop, 8188 MiB | Intel Arc A370M |
| Integrated GPU | Intel UHD Graphics | Intel Iris Xe (Alder Lake-P GT2) |
| RAM | 16 GiB: 2 × 8 GiB SK hynix HMCG66AEBSA095N @ 4800 MT/s | 16 GB; kernel sees 15.3 GiB (iGPU reserve) |
| Swap | Windows pagefile | 7.7 GiB zram + disk swap (23.7 GiB total) |
| Internal SSD | SK hynix BC901 512 GB | SK hynix HFM001TD3JX013N, 1 TB |
| Other storage | Hynix PSSD X31 1 TB (external USB) | — |
| Partitions | — | Dual boot: 642 GiB BitLocker (Windows) + 310 GiB ext4 `/` |
| Display | BOE panel, 1920 × 1080 @ 165 Hz | Samsung 0x416E OLED, 2880 × 1620 @ 120 Hz (60 Hz also) |
| Panel size | 34 × 19 cm image area (15.6-inch class) | 34 × 19 cm image area (15.6-inch class) |
| Battery | Not measured | 96.8 Wh design, 82.3 Wh now (85% health) |
| Firmware | BIOS 1.34.0 | BIOS UP6502ZD.303 |
| OS | Windows 11 Home 10.0.26300 | Arch Linux, kernel 7.2.9-arch1-1 |
| Desktop | Windows shell | Hyprland 0.56.2 |
| Driver | NVIDIA 617.42 | i915/xe (in-kernel) |

## Corrections to earlier notes

- The ASUS screen is the 15.6-inch class panel, not 16-inch: EDID reports a
  340 × 190 mm image area (≈15.3-inch measured diagonal, marketed as 15.6).
- The earlier "304 GiB" ASUS figure was the ext4 root partition. The drive is
  1 TB and also holds a BitLocker-encrypted Windows install.
- Dell usable memory (16,849,256,448 bytes) differs from installed module total
  (17,179,869,184 bytes) because firmware/iGPU reserve some. Same effect on ASUS.
- `Win32_VideoController.AdapterRAM` caps at 4 GiB and is not used for VRAM;
  `nvidia-smi` reports 8188 MiB.
