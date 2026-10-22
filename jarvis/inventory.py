"""Read-only inventory. Never collect device serials, MACs, or product keys."""

import json
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=25,
                                encoding="utf-8", errors="replace", shell=False)
        if result.returncode:
            return {"status": "unavailable", "exit_code": result.returncode}
        text = result.stdout.strip().lstrip("\ufeff")
        try:
            return json.loads(text)
        except ValueError:
            return text
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def linux_displays():
    """Query only one unambiguous session, excluding screen serial numbers."""
    monitors = command(["hyprctl", "monitors", "-j"])
    if not isinstance(monitors, list):
        instances = command(["hyprctl", "instances", "-j"])
        if isinstance(instances, list) and len(instances) == 1:
            monitors = command(["hyprctl", "-i", instances[0]["instance"], "monitors", "-j"])
    if not isinstance(monitors, list):
        return {"status": "unavailable", "reason": "No unambiguous Hyprland session"}
    fields = ("name", "make", "model", "width", "height", "refreshRate",
              "physicalWidth", "physicalHeight", "scale", "availableModes")
    return [{key: monitor.get(key) for key in fields} for monitor in monitors]


def linux_batteries():
    fields = ("manufacturer", "model_name", "technology", "energy_full_design",
              "energy_full", "charge_full_design", "charge_full", "voltage_min_design")
    return [{**{"name": battery.name}, **{field: read_text(battery / field) for field in fields}}
            for battery in Path("/sys/class/power_supply").glob("*")
            if read_text(battery / "type") == "Battery"]


WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$data = [ordered]@{
 computer = Get-CimInstance Win32_ComputerSystem |
   Select-Object Manufacturer,Model,TotalPhysicalMemory
 cpu = Get-CimInstance Win32_Processor |
   Select-Object Name,NumberOfCores,NumberOfLogicalProcessors
 memory = @(Get-CimInstance Win32_PhysicalMemory |
   Select-Object Manufacturer,PartNumber,Capacity,Speed,ConfiguredClockSpeed)
 disks = @(Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size)
 os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version
 bios = Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion
 gpus = @(Get-CimInstance Win32_VideoController |
   Select-Object Name,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
 monitors = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID |
   ForEach-Object {
     [pscustomobject]@{
       Manufacturer = -join ($_.ManufacturerName | Where-Object {$_ -gt 0} | ForEach-Object {[char]$_})
       ProductCode = -join ($_.ProductCodeID | Where-Object {$_ -gt 0} | ForEach-Object {[char]$_})
       FriendlyName = -join ($_.UserFriendlyName | Where-Object {$_ -gt 0} | ForEach-Object {[char]$_})
       Active = $_.Active
     }
   })
}
$data | ConvertTo-Json -Depth 6
"""


def collect():
    data = {"collected_at": datetime.now(timezone.utc).isoformat(),
            "source": "live_local_probe", "platform": platform.system()}
    if platform.system() == "Windows":
        data["hardware"] = command(["powershell.exe", "-NoProfile", "-NonInteractive",
                                    "-Command", WINDOWS_SCRIPT])
        data["nvidia"] = command(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                  "--format=csv,noheader"])
    elif platform.system() == "Linux":
        data["hardware"] = {
            "manufacturer": read_text("/sys/class/dmi/id/sys_vendor"),
            "model": read_text("/sys/class/dmi/id/product_name"),
            "bios": read_text("/sys/class/dmi/id/bios_version"),
            "cpu": command(["lscpu", "--json"]),
            "memory": read_text("/proc/meminfo"),
            "disks": command(["lsblk", "--json", "--bytes", "--output",
                              "NAME,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS"]),
            "pci": command(["lspci"]),
            "os": read_text("/etc/os-release"),
            "displays": linux_displays(),
            "batteries": linux_batteries(),
            "kernel": platform.release(),
        }
        data["notes"] = [
            "SSH sessions may not expose Hyprland's session socket.",
            "Physical memory module speed and battery design capacity require separate verification.",
            "Disk sizes are bytes; partitions and physical drives are distinct."
        ]
    else:
        raise ValueError("Inventory currently supports Windows and Linux.")
    data["tools"] = {name: shutil.which(name) is not None for name in
                     ("codex", "claude", "ssh", "tailscale", "sunshine", "moonlight", "lan-mouse")}
    return data


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
