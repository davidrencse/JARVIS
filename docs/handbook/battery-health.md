# Battery health and machine readings

Each computer shows the same reading on the console Machines tab, on its Connections card, and on the phone's device screen. The phone has no separate stats screen: the block sits under Control and Camera, so those actions stay first, and Computer details stays the name, role and addresses.

The backend computes every figure. The page only prints it. A missing field stays missing.

## Charge is not health

Battery charge is how full the battery is right now, plus whether it is charging, discharging, full, or on AC. Battery health is full-charge capacity divided by design capacity. Both numbers have to be present and in the same unit. Energy (µWh) is never divided by charge (µAh).

Nothing estimates health from the current charge, from how long the machine has been running, or from voltage. If the design capacity was not reported, health says Unavailable and why. A desktop or a cloud machine with no battery says Not applicable. An old inventory that has no battery section says Not reported. Those are not failures.

Linux reads `/sys/class/power_supply`, bounded and read-only. Windows asks Win32_Battery and, when both sides report a capacity, `root\wmi` BatteryStaticData and BatteryFullChargedCapacity. If those two ratios disagree by more than 5 percent, neither is chosen. The script does not write a battery report, does not use `powercfg`, and does not ask for elevation. Cycle count is shown only when the machine reports one. A missing cycle count is left out, not shown as zero.

A health figure above 100 percent is shown as "of design" and is not clamped. Above 150 percent it is still shown, with "check units". Degradation is the gap from that same rounded percent, so the two add to 100. Negative degradation means the full-charge capacity is above the design capacity. A charge percent outside 0–100 is Unavailable, with the raw number beside it, and is not clamped.

Two batteries stay two batteries. The pack line is the design-weighted sum, and only when every battery reported health in the same unit. One battery is not repeated as a pack plus itself.

## RAM, swap, disk, CPU

The four groups are Used, Free and Total. Free plus Used equals Total. GB is 1024³, the same unit `free -h` prints, with trailing zeros removed (86.0 is shown as 86 GB).

Linux RAM free is MemAvailable: memory the kernel can give out, including cache it can reclaim. MemFree is not used. Windows RAM free is FreePhysicalMemory. That is memory Windows calls free, not Linux available memory. The detail line says which one it is.

Swap free is unused swap. No swap, or a zero-size page file, is Not applicable and the group is still shown. If reported free is larger than total, the group is Unavailable. It is not clamped into range. On Windows, page-file use larger than the allocated size is the same kind of Unavailable.

Disk free is the space this account can use. Root-reserved blocks are not counted as free, and Used is Total minus that Free, so the reserved space sits in Used. The total is the filesystem that was measured (the system volume, and Home only when it is a different filesystem). It is not the whole physical drive. Letters are kept (`C:`). Volume labels, home paths and usernames are not.

CPU on Linux is Load: the 1-minute load average divided by the logical CPU count. It is not a sampled busy percent, and it is not clamped at 100. If the core count is missing, the load average is shown on its own and is not called a percent. CPU on Windows is Busy: Win32_Processor LoadPercentage. The two are different measurements and are labeled differently.

## How old the numbers are

Fresh is 30 minutes or less, aging is 6 hours or less, and stale is older than that. No timestamp is "time unknown". A timestamp more than two minutes in the future is "time doesn't agree". Offline is whether the computer is checking in. An offline machine keeps its last numbers and says they are the last reading. The screen does not invent new ones.

Inventory is not collected on a timer. A saved inventory stays stale until someone runs `jarvis inventory`, or until the node is polling with this code and the heartbeat carries a newer reading. The heartbeat is cached for about 20 seconds on the node so a poll does not re-read the machine every time. The first Windows read can take the CIM timeout (8 seconds). It is a separate query from the rest of inventory, so a hang there does not blank the hardware list.

There is no notification and no automatic action from these numbers.

## What is not in the reading

No serial number, MAC, product key, Windows UniqueID, or battery DeviceID. No home directory path and no username. A battery is "BAT0" or "Battery 1".

The Dell's battery was not read for this note. `docs/hardware.md` still records that battery as not measured. Linux health can be computed on the main from an inventory that already stored `energy_full` and `energy_full_design`, without updating the node. Charge percent, Windows capacities, and live RAM, swap, disk and CPU on a node show up after that node is running this code.

## What you have to restart

Restart JARVIS on the main so the console and the hub pick up the Python. Reload the console page for the new script and CSS. The phone picks up `/meters.js` on its next load. A node does not send the new Windows fields until `jarvis cluster update <node>`. Do not expect a live Dell battery figure before that update.
