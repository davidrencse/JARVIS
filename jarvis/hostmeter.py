"""Read-only RAM, swap, disk, CPU load and battery health for one computer.

Charge percent is not health. Health is full-charge capacity divided by design
capacity, and only when both numbers exist in the same unit. Nothing here
guesses health from the current charge, a runtime estimate, or voltage.

The same summary rides the inventory file and the node heartbeat. Callers
pass fixtures in tests; `sample()` is the only function that touches this
machine. GB in the labels is 1024^3, the figure `free -h` prints.
"""

import copy
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .platforms import NO_WINDOW

FRESH_S = 30 * 60
AGING_S = 6 * 3600
PLAUSIBLE_MAX = 150.0  # shown either way; above this the label says to check units
MAX_SUPPLIES = 8
MAX_DISKS = 8
READ_LIMIT = 64
_GIB = 1024 ** 3
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
_NUM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_CACHE = {"at": 0.0, "raw": None}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 20  # Linux /proc read only; the node poll is longer than this
_SAMPLE_TTL = 10  # Windows CIM meters: a 5 s console poll and the node share one query
_FAIL_TTL = 5  # a fast CIM failure is not retried in a tight loop, and recovers quickly

# One fixed script. No format fields, so nothing the node knows is spliced in.
WINDOWS_METERS_SCRIPT = r"""
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
function Clip([string]$Text) {
  if (-not $Text) { return '' }
  if ($Text.Length -gt 160) { return $Text.Substring(0, 160) }
  return $Text
}
function Read-Class([string]$Class, [string]$Namespace) {
  try {
    $splat = @{ ClassName = $Class; ErrorAction = 'Stop'; OperationTimeoutSec = 4 }
    if ($Namespace) { $splat.Namespace = $Namespace }
    $items = @(Get-CimInstance @splat | Select-Object -First 8)
    return @{ ok = $true; items = $items }
  } catch {
    return @{ ok = $false; error = $_.Exception.GetType().Name; message = (Clip ([string]$_.Exception.Message)); items = @() }
  }
}
$os = Read-Class 'Win32_OperatingSystem' $null
$mem = @{ ok = $os.ok; error = $os.error; message = $os.message }
if ($os.ok -and $os.items.Count -gt 0) {
  $row = $os.items[0]
  $mem.total_kb = $row.TotalVisibleMemorySize
  $mem.free_kb = $row.FreePhysicalMemory
}
$pages = Read-Class 'Win32_PageFileUsage' $null
$pageItems = @()
if ($pages.ok) {
  foreach ($row in $pages.items) {
    $pageItems += @{ allocated_mb = $row.AllocatedBaseSize; usage_mb = $row.CurrentUsage }
  }
}
$disks = Read-Class 'Win32_LogicalDisk' $null
$diskItems = @()
if ($disks.ok) {
  foreach ($row in $disks.items) {
    if ($row.DriveType -ne 3) { continue }
    $letter = [string]$row.DeviceID
    if ($letter -notmatch '^[A-Z]:$') { continue }
    $diskItems += @{ name = $letter; total = $row.Size; free = $row.FreeSpace }
  }
}
$cpu = Read-Class 'Win32_Processor' $null
$cpuItems = @()
if ($cpu.ok) {
  foreach ($row in $cpu.items) {
    $cpuItems += @{ load = $row.LoadPercentage; cores = $row.NumberOfLogicalProcessors }
  }
}
$win = Read-Class 'Win32_Battery' $null
$winItems = @()
if ($win.ok) {
  $i = 0
  foreach ($row in $win.items) {
    $i += 1
    $winItems += @{
      name = ('Battery {0}' -f $i)
      charge_percent = $row.EstimatedChargeRemaining
      status_code = $row.BatteryStatus
      design_mwh = $row.DesignCapacity
      full_mwh = $row.FullChargeCapacity
    }
  }
}
$static = Read-Class 'BatteryStaticData' 'root\wmi'
$staticItems = @()
if ($static.ok) {
  foreach ($row in $static.items) { $staticItems += @{ design_mwh = $row.DesignedCapacity } }
}
$full = Read-Class 'BatteryFullChargedCapacity' 'root\wmi'
$fullItems = @()
if ($full.ok) {
  foreach ($row in $full.items) { $fullItems += @{ full_mwh = $row.FullChargedCapacity } }
}
$power = Read-Class 'BatteryStatus' 'root\wmi'
$powerItems = @()
if ($power.ok) {
  foreach ($row in $power.items) {
    $powerItems += @{ charging = [bool]$row.Charging; discharging = [bool]$row.Discharging; ac = [bool]$row.PowerOnline }
  }
}
$cycles = Read-Class 'BatteryCycleCount' 'root\wmi'
$cycleItems = @()
if ($cycles.ok) {
  foreach ($row in $cycles.items) { $cycleItems += @{ cycle_count = $row.CycleCount } }
}
@{
  memory = $mem
  pages = @{ ok = $pages.ok; error = $pages.error; message = $pages.message; items = $pageItems }
  disks = @{ ok = $disks.ok; error = $disks.error; message = $disks.message; items = $diskItems }
  cpu = @{ ok = $cpu.ok; error = $cpu.error; message = $cpu.message; items = $cpuItems }
  win32 = @{ ok = $win.ok; error = $win.error; message = $win.message; items = $winItems }
  static = @{ ok = $static.ok; error = $static.error; message = $static.message; items = $staticItems }
  full = @{ ok = $full.ok; error = $full.error; message = $full.message; items = $fullItems }
  power = @{ ok = $power.ok; error = $power.error; message = $power.message; items = $powerItems }
  cycles = @{ ok = $cycles.ok; error = $cycles.error; message = $cycles.message; items = $cycleItems }
} | ConvertTo-Json -Depth 6 -Compress
"""


def _now(now):
    return time.time() if now is None else float(now)


def _iso(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat()


def _stamp(measured_at, now):
    """(age_s or None, freshness, when-label)."""
    if not measured_at:
        return None, "unknown", "time unknown"
    try:
        text = str(measured_at).replace("Z", "+00:00")
        at = datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None, "unknown", "time unknown"
    age = _now(now) - at
    if age < -120:
        return int(age), "unknown", "time doesn't agree"
    if age < 0:
        age = 0
    if age <= FRESH_S:
        fresh = "fresh"
    elif age <= AGING_S:
        fresh = "aging"
    else:
        fresh = "stale"
    return int(age), fresh, _ago(age)


def _ago(seconds):
    seconds = int(seconds)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds} s ago"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = round(minutes / 60)
    if hours < 48:
        return f"{hours} h ago"
    return f"{round(hours / 24)} d ago"


def _num(value):
    """(float or None, reason or None). Bool and junk are not numbers."""
    if isinstance(value, bool) or value is None:
        return None, "missing" if value is None else "not_a_number"
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return None, "not_a_number"
        return float(value), None
    text = str(value).strip()
    if not text:
        return None, "missing"
    if not _NUM.fullmatch(text):
        return None, "not_a_number"
    return float(text), None


def _trim(value, places=1):
    text = f"{float(value):.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _gb(nbytes):
    number, reason = _num(nbytes)
    if reason or number is None or number < 0:
        return None
    return f"{_trim(number / _GIB)} GB"


def _reason_text(code, what):
    return {
        "missing": f"{what} not reported",
        "not_a_number": f"{what} is not a number",
        "negative": f"{what} is negative",
        "zero": f"{what} is zero",
        "zero_design": "design capacity is zero",
        "zero_full": "full-charge capacity is zero",
        "unreadable": f"{what} is not readable",
        "too_long": f"{what} is not a number",
        "out_of_range": f"{what} is outside 0–100",
        "units": "energy and charge units differ",
        "disagree": "energy and charge health disagree",
        "timeout": "the reading timed out",
    }.get(code, f"{what} unavailable")


def _read_bounded(path):
    """(text or None, error code or None). Follows a sysfs directory symlink, not a file symlink."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except OSError as exc:
        return None, "unreadable" if exc.errno in (13, 1) else type(exc).__name__
    try:
        data = os.read(fd, READ_LIMIT + 1)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    if len(data) > READ_LIMIT:
        return None, "too_long"
    return data.decode("utf-8", "replace").strip("\x00\n\r \t"), None


def _pair(full, design):
    """Health ratio for one unit family, or an error dict. Never mixes families."""
    full_n, full_e = _num(full)
    design_n, design_e = _num(design)
    if full_e == "missing" and design_e == "missing":
        return None
    if full_e == "missing":
        return {"error": "missing", "what": "full-charge capacity", "design": design_n}
    if design_e == "missing":
        return {"error": "missing", "what": "design capacity", "full": full_n}
    if full_e or design_e:
        return {"error": "not_a_number", "what": "capacity"}
    if full_n < 0 or design_n < 0:
        return {"error": "negative", "what": "capacity", "full": full_n, "design": design_n}
    if design_n == 0:
        return {"error": "zero_design", "full": full_n, "design": 0.0}
    if full_n == 0:
        return {"error": "zero_full", "full": 0.0, "design": design_n}
    ratio = full_n / design_n
    percent = round(ratio * 100, 1)
    return {"full": full_n, "design": design_n, "percent": percent,
            "above_design": percent > 100, "plausible": percent <= PLAUSIBLE_MAX}


def _agree(a, b):
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale <= 0.05


def _health_from_pairs(energy, charge):
    """Pick one unit family. Disagreeing ratios are not averaged."""
    ok_e = energy and "percent" in energy
    ok_c = charge and "percent" in charge
    if ok_e and ok_c:
        if not _agree(energy["percent"], charge["percent"]):
            return {"error": "disagree", "what": "capacity",
                    "detail": f"energy {_trim(energy['percent'])}% and charge {_trim(charge['percent'])}%"}
        return {**energy, "unit": "uWh"}
    if ok_e:
        return {**energy, "unit": "uWh"}
    if ok_c:
        return {**charge, "unit": "uAh"}
    failed = energy or charge
    if not failed:
        return {"error": "missing", "what": "design capacity"}
    return failed


def _cycle(value, error=None):
    if error:
        return None, "cycle count is not readable"
    if value is None or value == "":
        return None, None  # optional: silence, not a failure
    number, reason = _num(value)
    if reason:
        return None, "cycle count is not a number"
    if number < 0 or number != int(number):
        return None, "cycle count is not a whole number"
    return int(number), None


def _charge_percent(value, error=None):
    if error:
        return None, "charge percent is not readable", _safe(value)
    number, reason = _num(value)
    if reason == "missing":
        return None, None, None
    if reason:
        return None, "charge percent is not a number", _safe(value)
    if number < 0 or number > 100:
        return None, "charge percent is outside 0–100", _trim(number, 1)
    return round(number, 1), None, None


def _safe(value):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = re.sub(r"[A-Za-z]:\\[^\s]+", "path", text)
    text = re.sub(r"/home/\S+", "path", text)
    return text[:160] or None


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.()-]{0,31}\Z")


def _supply_name(value, fallback):
    """A battery label. Sysfs names are short tokens; Windows uses 'Battery 1'. Paths are dropped."""
    text = _safe(value)
    if text and _LABEL.fullmatch(text) and text != "path":
        return text
    return fallback


def _clean_errors(errors):
    """Keep only the field names that failed. The message itself is not stored."""
    if not isinstance(errors, dict):
        return None
    allowed = ("status", "capacity", "energy_full", "energy_full_design", "charge_full",
               "charge_full_design", "cycle_count")
    clean = {key: "unreadable" for key in allowed if errors.get(key)}
    return clean or None


def _power_from_status(status):
    key = str(status or "").strip().lower()
    return {
        "charging": "charging", "discharging": "discharging", "full": "full",
        "not charging": "ac", "unknown": "unknown",
    }.get(key, "unknown" if key else None)


_WIN_STATUS = {
    1: "discharging", 2: "ac", 3: "full", 4: "discharging", 5: "discharging",
    6: "charging", 7: "charging", 8: "charging", 9: "charging", 11: "discharging",
}


def _cell(name, power, charge, charge_reason, charge_raw, health, unit, cycle, cycle_reason,
          full=None, design=None):
    percent = health.get("percent") if health and "percent" in health else None
    return {
        "name": name or "Battery",
        "power": power or "unknown",
        "charge_percent": charge,
        "charge_reason": charge_reason,
        "charge_raw": charge_raw,
        "health_percent": percent,
        "health_reason": None if percent is not None else _health_reason(health),
        "health_detail": (health or {}).get("detail"),
        "above_design": bool((health or {}).get("above_design")),
        "plausible": (health or {}).get("plausible") if percent is not None else None,
        "degradation_percent": None if percent is None else round(100 - percent, 1),
        "full": full if full is not None else (health or {}).get("full"),
        "design": design if design is not None else (health or {}).get("design"),
        "unit": unit if percent is not None else (health or {}).get("unit"),
        "cycle_count": cycle,
        "cycle_reason": cycle_reason,
    }


def _health_reason(health):
    if not health:
        return "design capacity not reported"
    if health.get("detail"):
        return health["detail"] if health.get("error") == "disagree" else _reason_text(health.get("error"), health.get("what") or "capacity")
    return _reason_text(health.get("error") or "missing", health.get("what") or "design capacity")


def _linux_supply(entry):
    """One sysfs battery dict (strings plus an errors map) -> cell."""
    errors = entry.get("errors") if isinstance(entry.get("errors"), dict) else {}
    energy = _pair(entry.get("energy_full"), entry.get("energy_full_design"))
    charge = _pair(entry.get("charge_full"), entry.get("charge_full_design"))
    if errors.get("energy_full_design") or errors.get("energy_full"):
        energy = {"error": "unreadable", "what": "capacity"}
    if errors.get("charge_full_design") or errors.get("charge_full"):
        charge = {"error": "unreadable", "what": "capacity"}
    # A present error on only one side of a pair that is otherwise missing stays missing.
    health = _health_from_pairs(energy, charge)
    if health and health.get("error") == "unreadable":
        pass
    elif errors.get("energy_full_design") and not (energy and "percent" in (energy or {})) and not (charge and "percent" in (charge or {})):
        health = {"error": "unreadable", "what": "design capacity"}
    percent, why, raw = _charge_percent(entry.get("capacity"), errors.get("capacity"))
    cycle, cycle_why = _cycle(entry.get("cycle_count"), errors.get("cycle_count"))
    unit = health.get("unit") if health else None
    full = design = None
    if unit == "uWh":
        full, design = health.get("full"), health.get("design")
    elif unit == "uAh":
        full, design = health.get("full"), health.get("design")
    return _cell(entry.get("name"), _power_from_status(entry.get("status")), percent, why, raw,
                 health, unit, cycle, cycle_why, full, design)


def _windows_supply(entry):
    wmi = _pair(entry.get("wmi_full_mwh"), entry.get("wmi_design_mwh"))
    win = _pair(entry.get("win_full_mwh"), entry.get("win_design_mwh"))
    ok_w, ok_n = wmi and "percent" in wmi, win and "percent" in win
    if ok_w and ok_n and not _agree(wmi["percent"], win["percent"]):
        health = {"error": "disagree", "what": "capacity", "unit": "mWh",
                  "detail": f"root\\wmi {_trim(wmi['percent'])}% and Win32_Battery {_trim(win['percent'])}%"}
        full = design = unit = None
    elif ok_w:
        health, unit = {**wmi, "unit": "mWh"}, "mWh"
        full, design = wmi["full"], wmi["design"]
    elif ok_n:
        health, unit = {**win, "unit": "mWh"}, "mWh"
        full, design = win["full"], win["design"]
    else:
        health = wmi or win or {"error": "missing", "what": "design capacity"}
        unit = full = design = None
    percent, why, raw = _charge_percent(entry.get("charge_percent"))
    power = entry.get("power")
    if power not in ("charging", "discharging", "full", "ac", "unknown"):
        code, code_e = _num(entry.get("status_code"))
        power = _WIN_STATUS.get(int(code)) if code_e is None else None
    cycle, cycle_why = _cycle(entry.get("cycle_count"), entry.get("cycle_error"))
    return _cell(entry.get("name"), power, percent, why, raw, health, unit, cycle, cycle_why, full, design)


def _pack(cells):
    if not cells:
        return None
    if len(cells) == 1:
        one = dict(cells[0])
        one["name"] = None
        return one
    pack = {"name": None, "power": _pack_power(cells)}
    units = {c.get("unit") for c in cells if c.get("health_percent") is not None}
    if any(c.get("health_percent") is None for c in cells):
        missing = [c["name"] for c in cells if c.get("health_percent") is None]
        pack.update(health_percent=None, health_reason="not every battery reported health (" + ", ".join(missing) + ")",
                    above_design=False, plausible=None, degradation_percent=None, full=None, design=None, unit=None)
    elif len(units) != 1:
        pack.update(health_percent=None, health_reason="batteries use different capacity units",
                    above_design=False, plausible=None, degradation_percent=None, full=None, design=None, unit=None)
    else:
        full = sum(c["full"] for c in cells)
        design = sum(c["design"] for c in cells)
        health = _pair(full, design)
        percent = health.get("percent") if health else None
        plausible = bool(health and health.get("plausible") and all(c.get("plausible") for c in cells))
        pack.update(health_percent=percent, health_reason=None if percent is not None else _health_reason(health),
                    above_design=bool(health and health.get("above_design")), plausible=plausible if percent is not None else None,
                    degradation_percent=None if percent is None else round(100 - percent, 1),
                    full=full, design=design, unit=next(iter(units)))
    if any(c.get("charge_percent") is None for c in cells):
        pack["charge_percent"] = None
        pack["charge_reason"] = "not every battery reported charge"
    elif len({c.get("unit") for c in cells if c.get("full")}) > 1 or any(not c.get("full") for c in cells):
        pack["charge_percent"] = None
        pack["charge_reason"] = "can't weight charge across batteries"
    else:
        weight = sum(c["full"] for c in cells)
        pack["charge_percent"] = round(sum(c["charge_percent"] * c["full"] for c in cells) / weight, 1)
        pack["charge_reason"] = None
    if all(c.get("cycle_count") is not None for c in cells):
        pack["cycle_count"] = sum(c["cycle_count"] for c in cells)
        pack["cycle_reason"] = None
    elif any(c.get("cycle_count") is not None for c in cells):
        pack["cycle_count"] = None
        pack["cycle_reason"] = "not reported for every battery"
    else:
        pack["cycle_count"] = None
        pack["cycle_reason"] = None
    return pack


def _pack_power(cells):
    powers = {c.get("power") or "unknown" for c in cells}
    if len(powers) == 1:
        return powers.pop()
    if "discharging" in powers and "charging" in powers:
        return "mixed"
    if "discharging" in powers:
        return "discharging"
    if "charging" in powers:
        return "charging"
    return "mixed"


def _cap_text(full, design, unit):
    scale = {"uWh": (1_000_000, "Wh"), "uAh": (1_000_000, "Ah"), "mWh": (1000, "Wh"), "mAh": (1000, "Ah")}.get(unit)
    if not scale or full is None or design is None:
        return None
    div, label = scale
    return f"{_trim(full / div)} {label} full · {_trim(design / div)} {label} design"


def _health_text(cell):
    if cell.get("health_percent") is None:
        return None
    text = _trim(cell["health_percent"]) + "%"
    if cell.get("above_design"):
        text += " of design"
    if cell.get("plausible") is False:
        text += " (check units)"
    return text


def _degrade_text(cell):
    value = cell.get("degradation_percent")
    if value is None:
        return None
    if value > 0:
        return f"{_trim(value)}% below design"
    if value < 0:
        return f"{_trim(abs(value))}% above design"
    return "none"


_POWER_TEXT = {
    "charging": "charging", "discharging": "discharging", "full": "full",
    "ac": "on AC", "mixed": "mixed", "unknown": "power state unknown",
}


def _battery_blocks(battery, reading):
    """Charge and health blocks. One battery is not repeated as a pack plus itself."""
    state = battery.get("applicability")
    if state == "not_applicable":
        return [{"title": "Battery", "wide": True, "rows": [["Status", "Not applicable", battery.get("reason") or "No battery reported"]]}]
    if state == "unreported":
        return [{"title": "Battery", "wide": True, "rows": [["Status", "Not reported", battery.get("reason") or "No battery section in this reading"]]}]
    if state == "unavailable" and not battery.get("cells"):
        return [{"title": "Battery", "wide": True, "rows": [["Status", "Unavailable", battery.get("reason") or "Battery could not be read"]]}]
    cells = battery.get("cells") or []
    pack = battery.get("pack") or (cells[0] if len(cells) == 1 else None)
    blocks = []
    if pack:
        blocks.extend(_one_battery_blocks(pack, "Battery charge", "Battery health", reading if len(cells) <= 1 else None))
    if len(cells) > 1:
        if not pack or pack.get("health_percent") is None or pack.get("charge_percent") is None:
            pass
        for cell in cells:
            blocks.extend(_one_battery_blocks(cell, f"{cell['name']} charge", f"{cell['name']} health", None))
        if reading and len(cells) > 1:
            blocks.append({"title": "Reading", "wide": True, "rows": reading})
    return blocks


def _one_battery_blocks(cell, charge_title, health_title, reading):
    charge = cell.get("charge_percent")
    if charge is None:
        detail = cell.get("charge_reason") or "charge percent not reported"
        if cell.get("charge_raw"):
            detail = f"{detail} · reported {cell['charge_raw']}"
        charge_row = ["Now", "Unavailable", detail]
    else:
        state = _POWER_TEXT.get(cell.get("power") or "", "")
        shown = _trim(charge) + "%" + (f" · {state}" if state else "")
        detail = f"reported {cell['charge_raw']}" if cell.get("charge_raw") else None
        charge_row = ["Now", shown, detail]
    if cell.get("health_percent") is None:
        health_row = ["Health", "Unavailable", cell.get("health_detail") or cell.get("health_reason")]
    else:
        detail = "Full-charge capacity is above 150% of design. The percent is not clamped." if cell.get("plausible") is False else cell.get("health_detail")
        health_row = ["Health", _health_text(cell), detail]
    rows = [health_row]
    cap = _cap_text(cell.get("full"), cell.get("design"), cell.get("unit"))
    if cap:
        raw = None
        if cell.get("full") is not None and cell.get("design") is not None and cell.get("unit"):
            raw = f"full {_trim(cell['full'], 0)} {cell['unit']} · design {_trim(cell['design'], 0)} {cell['unit']}"
        rows.append(["Capacity", cap, raw])
    degrade = _degrade_text(cell)
    if degrade:
        rows.append(["Degradation", degrade, "Compared with design capacity, not with the current charge"])
    if cell.get("cycle_count") is not None:
        rows.append(["Cycles", str(cell["cycle_count"]), None])
    elif cell.get("cycle_reason"):
        rows.append(["Cycles", "Unavailable", cell["cycle_reason"]])
    if reading:
        rows.extend(reading)
    return [
        {"title": charge_title, "wide": True, "rows": [charge_row]},
        {"title": health_title, "wide": True, "rows": rows},
    ]


def _bytes_block(title, part, free_means):
    if not isinstance(part, dict) or part.get("applicability") in ("unreported", None) and "total" not in part:
        reason = (part or {}).get("reason") or "not reported"
        state = "Not reported" if (part or {}).get("applicability") in (None, "unreported") else "Unavailable"
        if (part or {}).get("applicability") == "not_applicable":
            state, reason = "Not applicable", (part or {}).get("reason") or "none"
        return {"title": title, "wide": False, "rows": [["Status", state, reason]]}
    if part.get("applicability") == "not_applicable":
        return {"title": title, "wide": False, "rows": [["Status", "Not applicable", part.get("reason") or "none"]]}
    if part.get("applicability") == "unavailable" or part.get("total") is None:
        return {"title": title, "wide": False, "rows": [["Status", "Unavailable", part.get("reason") or "not reported"]]}
    total, free = part.get("total"), part.get("free")
    free_n, free_e = _num(free)
    total_n, total_e = _num(total)
    if total_e or free_e or total_n is None or free_n is None:
        return {"title": title, "wide": False, "rows": [["Status", "Unavailable", "the sizes are not numbers"]]}
    if total_n < 0 or free_n < 0:
        return {"title": title, "wide": False, "rows": [["Status", "Unavailable", "a size is negative"]]}
    if free_n > total_n:
        return {"title": title, "wide": False, "rows": [["Status", "Unavailable", "free is larger than total"],
                                                       ["Total", _gb(total_n) or "—", None]]}
    used = total_n - free_n
    return {"title": title, "wide": False, "rows": [
        ["Used", _gb(used) or "—", None],
        ["Free", _gb(free_n) or "—", free_means],
        ["Total", _gb(total_n) or "—", None],
    ]}


def _cpu_block(cpu):
    cpu = cpu or {}
    if cpu.get("applicability") in ("unreported", None) and "load1" not in cpu and "busy_percent" not in cpu:
        return {"title": "CPU", "wide": False, "rows": [["Status", "Not reported", cpu.get("reason") or "no load reading"]]}
    if cpu.get("applicability") == "unavailable":
        return {"title": "CPU", "wide": False, "rows": [["Status", "Unavailable", cpu.get("reason") or "CPU could not be read"]]}
    rows = []
    load, load_e = _num(cpu.get("load1"))
    cores, cores_e = _num(cpu.get("cores"))
    if load_e is None and load is not None and load < 0:
        load_e = "negative"
    if load_e is None and cores_e is None and cores and cores > 0:
        share = load / cores * 100
        rows.append(["Load", _trim(share, 0) + "%", f"{_trim(load, 2)} on {_trim(cores, 0)} cores. This is the 1-minute load average shared across CPUs, not a sampled busy percent."])
    elif load_e is None:
        rows.append(["Load", _trim(load, 2), "logical CPU count not reported, so this is not a percent"])
    busy, busy_e = _num(cpu.get("busy_percent"))
    if busy_e is None:
        if busy < 0:
            rows.append(["Busy", "Unavailable", "busy percent is negative"])
        else:
            rows.append(["Busy", _trim(busy, 0) + "%", "Windows LoadPercentage. Not clamped."])
    if not rows:
        return {"title": "CPU", "wide": False, "rows": [["Status", "Unavailable", cpu.get("reason") or "no load reading"]]}
    return {"title": "CPU", "wide": False, "rows": rows}


def _reading_rows(raw, now, presence):
    age, fresh, when = _stamp(raw.get("measured_at"), now)
    source = {"sysfs": "Linux sysfs and /proc", "cim": "Windows CIM", "probe": "SSH status",
              "inventory": "saved inventory"}.get(raw.get("source") or "", raw.get("source") or "unknown source")
    rows = [["When", f"{when} · {fresh}", None], ["Source", source, None]]
    if presence == "offline":
        rows.append(["Link", "Offline", "The computer is not checking in. Figures are the last reading, not a live sample."])
    elif presence == "online":
        rows.append(["Link", "Online", None])
    return rows, age, fresh


def view(raw, now=None, presence=None):
    """Display block list plus the numbers it came from. Does no extra I/O."""
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("applicability") in ("unreported", "unavailable") and "ram" not in raw and "battery" not in raw:
        state = "Not reported" if raw.get("applicability") != "unavailable" else "Unavailable"
        return {"applicability": raw.get("applicability") or "unreported", "reason": raw.get("reason"),
                "measured_at": None, "freshness": "unknown", "blocks": [
                    {"title": "Stats", "wide": True, "rows": [["Status", state, raw.get("reason") or "No hardware reading"]]}]}
    battery = _normalize_battery(raw.get("battery") or {})
    ram = raw.get("ram") if isinstance(raw.get("ram"), dict) else {"applicability": "unreported", "reason": "not reported"}
    swap = raw.get("swap") if isinstance(raw.get("swap"), dict) else {"applicability": "unreported", "reason": "not reported"}
    disks = raw.get("disks") if isinstance(raw.get("disks"), list) else []
    cpu = raw.get("cpu") if isinstance(raw.get("cpu"), dict) else {"applicability": "unreported"}
    reading, age, fresh = _reading_rows(raw, now, presence)
    blocks = [
        _bytes_block("RAM", ram, ram.get("free_means")),
        _bytes_block("Swap", swap, swap.get("free_means")),
    ]
    if not disks:
        blocks.append({"title": "Disk", "wide": False, "rows": [["Status", "Not reported", "no disk reading"]]})
    for disk in disks[:MAX_DISKS]:
        title = "Disk" if disk.get("name") in (None, "", "system") else "Disk · " + str(disk.get("name"))[:32]
        blocks.append(_bytes_block(title, disk, "Free is the space this account can use."))
    blocks.append(_cpu_block(cpu))
    blocks.extend(_battery_blocks(battery, reading))
    if battery.get("applicability") in ("not_applicable", "unreported", "unavailable") and not battery.get("cells"):
        blocks.append({"title": "Reading", "wide": True, "rows": reading})
    return {
        "applicability": "present", "measured_at": raw.get("measured_at"), "age_s": age,
        "freshness": fresh, "source": raw.get("source"), "presence": presence,
        "blocks": blocks, "battery": battery, "ram": ram, "swap": swap,
        "disks": disks[:MAX_DISKS], "cpu": cpu,
    }


def _normalize_battery(battery):
    if not isinstance(battery, dict):
        return {"applicability": "unreported", "reason": "no battery section", "cells": []}
    state = battery.get("applicability")
    supplies = battery.get("supplies") if isinstance(battery.get("supplies"), list) else []
    cells = []
    for entry in supplies[:MAX_SUPPLIES]:
        if not isinstance(entry, dict):
            continue
        if "energy_full" in entry or "energy_full_design" in entry or "charge_full" in entry or entry.get("kind") == "linux":
            cells.append(_linux_supply(entry))
        else:
            cells.append(_windows_supply(entry))
    if state == "not_applicable":
        return {"applicability": "not_applicable", "reason": battery.get("reason") or "No battery reported", "cells": []}
    if state == "unavailable" and not cells:
        return {"applicability": "unavailable", "reason": battery.get("reason") or "Battery could not be read", "cells": []}
    if state == "unreported" and not cells:
        return {"applicability": "unreported", "reason": battery.get("reason") or "No battery section in this reading", "cells": []}
    if not cells and state is None:
        return {"applicability": "unreported", "reason": "No battery section in this reading", "cells": []}
    pack = _pack(cells)
    return {"applicability": "present", "reason": None, "cells": cells, "pack": pack}


def _volume(total, free, name, reason=None, applicability=None):
    if applicability:
        return {"name": name, "applicability": applicability, "reason": reason}
    total_n, total_e = _num(total)
    free_n, free_e = _num(free)
    if total_e or free_e:
        return {"name": name, "applicability": "unavailable", "reason": "disk size is not a number"}
    return {"name": name, "total": total_n, "free": free_n, "unit": "B"}


def _mem_part(total, free, means, applicability=None, reason=None):
    if applicability:
        return {"applicability": applicability, "reason": reason, "free_means": means}
    total_n, total_e = _num(total)
    free_n, free_e = _num(free)
    if total_e == "missing" or free_e == "missing":
        return {"applicability": "unavailable", "reason": "memory size not reported", "free_means": means}
    if total_e or free_e:
        return {"applicability": "unavailable", "reason": "memory size is not a number", "free_means": means}
    return {"total": total_n, "free": free_n, "unit": "B", "free_means": means}


def linux_supplies(root):
    """Battery supplies under a power_supply directory. Does not compute health."""
    root = Path(root)
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except FileNotFoundError:
        return {"applicability": "not_applicable", "reason": "No power supply information.", "supplies": []}
    except OSError as exc:
        return {"applicability": "unavailable", "reason": f"power_supply is not readable ({type(exc).__name__}).", "supplies": []}
    supplies = []
    fields = ("status", "capacity", "energy_full", "energy_full_design", "energy_now",
              "charge_full", "charge_full_design", "charge_now", "cycle_count", "present",
              "manufacturer", "model_name", "technology", "voltage_min_design")
    for entry in entries[:32]:
        if not _NAME.fullmatch(entry.name):
            continue
        kind, kind_err = _read_bounded(entry / "type")
        if kind_err or kind != "Battery":
            continue
        present, present_err = _read_bounded(entry / "present")
        if not present_err and present == "0":
            continue
        item = {"kind": "linux", "name": entry.name, "errors": {}}
        for field in fields:
            if field == "present":
                continue
            text, err = _read_bounded(entry / field)
            if err == "FileNotFoundError":
                item[field] = None
            elif err:
                item[field] = None
                item["errors"][field] = "unreadable" if err == "unreadable" else err
            else:
                item[field] = text
        if not item["errors"]:
            item.pop("errors")
        supplies.append(item)
        if len(supplies) >= MAX_SUPPLIES:
            break
    if not supplies:
        return {"applicability": "not_applicable", "reason": "No battery reported.", "supplies": []}
    return {"applicability": "present", "supplies": supplies}


def legacy_battery_rows(battery):
    """Flat string rows, the shape inventory has always stored for Linux."""
    rows = []
    for entry in (battery or {}).get("supplies") or []:
        if not isinstance(entry, dict):
            continue
        rows.append({"name": entry.get("name"), **{k: entry.get(k) for k in (
            "manufacturer", "model_name", "technology", "energy_full_design", "energy_full",
            "charge_full_design", "charge_full", "voltage_min_design", "status", "capacity",
            "energy_now", "charge_now", "cycle_count")}})
    return rows


def _parse_meminfo(text):
    found = {}
    for line in (text or "").splitlines():
        match = re.match(r"^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+)\s+kB\s*$", line.strip())
        if match:
            found[match.group(1)] = int(match.group(2)) * 1024
    return found


def read_linux(proc_root="/proc", sys_root="/sys/class/power_supply", mounts=("/",)):
    """Canonical reading from /proc, sysfs and statvfs. `mounts` is a list of (name, path) or paths."""
    proc_root = Path(proc_root)
    ram = {"applicability": "unavailable", "reason": "MemTotal not reported",
           "free_means": "Free counts memory the kernel can give out, including reclaimable cache."}
    swap = {"applicability": "unavailable", "reason": "swap not reported", "free_means": "Swap free is unused swap."}
    try:
        info = _parse_meminfo((proc_root / "meminfo").read_text(encoding="utf-8", errors="replace")[:8000])
    except OSError as exc:
        info = {}
        ram = {"applicability": "unavailable", "reason": f"memory is not readable ({type(exc).__name__})", "free_means": ram["free_means"]}
        swap = {"applicability": "unavailable", "reason": ram["reason"], "free_means": swap["free_means"]}
    if info:
        if "MemTotal" in info and "MemAvailable" in info:
            ram = _mem_part(info["MemTotal"], info["MemAvailable"], ram["free_means"])
        elif "MemTotal" not in info:
            ram["reason"] = "MemTotal not reported"
        else:
            ram["reason"] = "MemAvailable not reported"
        if "SwapTotal" in info and info["SwapTotal"] == 0:
            swap = {"applicability": "not_applicable", "reason": "No swap is configured.", "free_means": swap["free_means"]}
        elif "SwapTotal" in info and "SwapFree" in info:
            swap = _mem_part(info["SwapTotal"], info["SwapFree"], swap["free_means"])
    load1 = None
    try:
        load_text = (proc_root / "loadavg").read_text(encoding="utf-8", errors="replace")[:80]
        load1, load_e = _num(load_text.split()[0] if load_text.split() else None)
        if load_e:
            load1 = None
    except OSError:
        load1 = None
    cores = os.cpu_count()
    cpu = {"load1": load1, "cores": cores}
    if load1 is None:
        cpu = {"applicability": "unavailable", "reason": "load average not reported", "cores": cores}
    disks = []
    seen = set()
    pairs = []
    for item in mounts:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((str(item[0])[:32], item[1]))
        else:
            pairs.append(("system" if str(item) == "/" else "volume", item))
    for name, path in pairs[:MAX_DISKS]:
        try:
            device = os.stat(path).st_dev
        except OSError:
            device = None
        if device is not None and device in seen:
            continue
        if device is not None:
            seen.add(device)
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            disks.append(_volume(None, None, name, f"disk is not readable ({type(exc).__name__})", "unavailable"))
            continue
        disks.append(_volume(usage.total, usage.free, name))
    battery = linux_supplies(sys_root)
    return {"v": 1, "measured_at": _iso(time.time()), "source": "sysfs",
            "ram": ram, "swap": swap, "disks": disks, "cpu": cpu, "battery": battery,
            "battery_rows": legacy_battery_rows(battery)}


def _section_error(section, fallback):
    if not isinstance(section, dict):
        return fallback
    if section.get("ok") is False:
        message = _safe(section.get("message")) or _safe(section.get("error")) or "query failed"
        if section.get("error") == "TimeoutExpired" or "timed out" in message.lower():
            return "the reading timed out"
        return message
    return None


def windows_canonical(payload, measured_at=None):
    """CIM JSON from WINDOWS_METERS_SCRIPT, or command()'s unavailable dict."""
    if not isinstance(payload, dict):
        return {"v": 1, "measured_at": measured_at, "source": "cim", "applicability": "unavailable",
                "reason": "battery and resource query returned nothing useful"}
    if payload.get("status") == "unavailable":
        reason = "the reading timed out" if payload.get("reason") == "TimeoutExpired" else "the resource query failed"
        return {"v": 1, "measured_at": measured_at, "source": "cim", "applicability": "unavailable", "reason": reason}
    mem = payload.get("memory") if isinstance(payload.get("memory"), dict) else {}
    mem_err = _section_error(mem, None)
    if mem_err:
        ram = {"applicability": "unavailable", "reason": mem_err,
               "free_means": "Free is physical memory Windows reports free."}
    else:
        total, free = mem.get("total_kb"), mem.get("free_kb")
        total_n, _ = _num(total)
        free_n, _ = _num(free)
        ram = _mem_part(None if total_n is None else total_n * 1024, None if free_n is None else free_n * 1024,
                        "Free is physical memory Windows reports free, not Linux available memory.")
    pages = payload.get("pages") if isinstance(payload.get("pages"), dict) else {}
    page_err = _section_error(pages, None)
    if page_err:
        swap = {"applicability": "unavailable", "reason": page_err, "free_means": "Swap is the page file."}
    else:
        items = [i for i in pages.get("items") or [] if isinstance(i, dict)]
        if not items:
            swap = {"applicability": "not_applicable", "reason": "No page file reported.", "free_means": "Swap is the page file."}
        else:
            allocated = used = 0.0
            bad = False
            for item in items[:MAX_DISKS]:
                alloc, a_e = _num(item.get("allocated_mb"))
                usage, u_e = _num(item.get("usage_mb"))
                if a_e or u_e or alloc is None or usage is None:
                    bad = True
                    break
                allocated += alloc
                used += usage
            if bad or allocated < 0:
                swap = {"applicability": "unavailable", "reason": "page file size is not a number", "free_means": "Swap is the page file."}
            elif allocated == 0:
                swap = {"applicability": "not_applicable", "reason": "No page file reported.", "free_means": "Swap is the page file."}
            elif used > allocated:
                swap = {"applicability": "unavailable", "reason": "page file use is larger than its size",
                        "free_means": "Swap is the Windows page file."}
            else:
                total_b = allocated * 1024 * 1024
                swap = _mem_part(total_b, (allocated - used) * 1024 * 1024, "Swap is the Windows page file.")
    disk_section = payload.get("disks") if isinstance(payload.get("disks"), dict) else {}
    disk_err = _section_error(disk_section, None)
    disks = []
    if disk_err:
        disks = [_volume(None, None, "system", disk_err, "unavailable")]
    else:
        for item in (disk_section.get("items") or [])[:MAX_DISKS]:
            if isinstance(item, dict):
                disks.append(_volume(item.get("total"), item.get("free"), _safe(item.get("name")) or "disk"))
        if not disks:
            disks = [_volume(None, None, "system", "No fixed disk reported.", "not_applicable")]
    cpu_section = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else {}
    cpu_err = _section_error(cpu_section, None)
    if cpu_err:
        cpu = {"applicability": "unavailable", "reason": cpu_err}
    else:
        loads, cores = [], 0.0
        missing_load = False
        for item in (cpu_section.get("items") or [])[:8]:
            if not isinstance(item, dict):
                continue
            count, count_e = _num(item.get("cores"))
            busy, busy_e = _num(item.get("load"))
            if count_e is None and count:
                cores += count
            if busy_e:
                missing_load = True
            elif busy is not None:
                loads.append((busy, count or 1))
        cpu = {"cores": cores or None, "load1": None}
        if loads and not missing_load:
            weight = sum(w for _, w in loads)
            cpu["busy_percent"] = round(sum(v * w for v, w in loads) / weight, 1)
        elif not loads:
            cpu["reason"] = "LoadPercentage not reported"
    battery = _windows_battery(payload)
    return {"v": 1, "measured_at": measured_at or _iso(time.time()), "source": "cim",
            "ram": ram, "swap": swap, "disks": disks, "cpu": cpu, "battery": battery}


def _windows_battery(payload):
    win = payload.get("win32") if isinstance(payload.get("win32"), dict) else {}
    err = _section_error(win, None)
    if err:
        return {"applicability": "unavailable", "reason": err, "supplies": []}
    items = [i for i in (win.get("items") or []) if isinstance(i, dict)]
    if not items:
        # A failed root\\wmi query with an empty Win32 list is still "no battery",
        # unless Win32 itself failed above.
        return {"applicability": "not_applicable", "reason": "No battery reported by Win32_Battery.", "supplies": []}
    static = _items(payload.get("static"))
    full = _items(payload.get("full"))
    power = _items(payload.get("power"))
    cycles = payload.get("cycles") if isinstance(payload.get("cycles"), dict) else {}
    cycle_items = _items(cycles) if cycles.get("ok") else []
    cycle_error = cycles.get("ok") is False
    paired = len(static) == len(items) and len(full) == len(items)
    supplies = []
    for index, item in enumerate(items[:MAX_SUPPLIES]):
        supply = {
            "kind": "windows", "name": _safe(item.get("name")) or f"Battery {index + 1}",
            "charge_percent": item.get("charge_percent"), "status_code": item.get("status_code"),
            "win_full_mwh": item.get("full_mwh"), "win_design_mwh": item.get("design_mwh"),
        }
        if paired:
            supply["wmi_design_mwh"] = static[index].get("design_mwh")
            supply["wmi_full_mwh"] = full[index].get("full_mwh")
        if len(power) == len(items):
            row = power[index]
            if row.get("charging"):
                supply["power"] = "charging"
            elif row.get("discharging"):
                supply["power"] = "discharging"
            elif row.get("ac"):
                supply["power"] = "ac"
        if len(cycle_items) == len(items):
            supply["cycle_count"] = cycle_items[index].get("cycle_count")
        elif cycle_error:
            supply["cycle_error"] = True
        supplies.append(supply)
    return {"applicability": "present", "supplies": supplies}


def _items(section):
    if not isinstance(section, dict):
        return []
    return [i for i in (section.get("items") or []) if isinstance(i, dict)]


def from_probe(kv, now=None):
    """Numbers already collected by the fixed SSH status script."""
    kv = kv or {}
    def kb(key):
        number, reason = _num(kv.get(key))
        if reason or number is None:
            return None
        return number * 1024
    total, avail = kb("mem_total_kb"), kb("mem_avail_kb")
    if total is None or avail is None:
        ram = {"applicability": "unreported" if "mem_total_kb" not in kv else "unavailable",
               "reason": "memory size not reported" if "mem_total_kb" not in kv else "memory size is not a number",
               "free_means": "Free counts memory the kernel can give out, including reclaimable cache."}
    else:
        ram = _mem_part(total, avail, "Free counts memory the kernel can give out, including reclaimable cache.")
    swap_total, swap_free = kb("swap_total_kb"), kb("swap_free_kb")
    if "swap_total_kb" not in kv:
        swap = {"applicability": "unreported", "reason": "swap not reported", "free_means": "Swap free is unused swap."}
    elif swap_total == 0:
        swap = {"applicability": "not_applicable", "reason": "No swap is configured.", "free_means": "Swap free is unused swap."}
    elif swap_total is None or swap_free is None:
        swap = {"applicability": "unavailable", "reason": "swap size is not a number", "free_means": "Swap free is unused swap."}
    else:
        swap = _mem_part(swap_total, swap_free, "Swap free is unused swap.")
    load, load_e = _num(kv.get("load"))
    cores, cores_e = _num(kv.get("nproc"))
    cpu = {"load1": None if load_e else load, "cores": None if cores_e else cores}
    if "load" not in kv and "nproc" not in kv:
        cpu = {"applicability": "unreported", "reason": "no load reading"}
    elif load_e and "load" in kv:
        cpu = {"applicability": "unavailable", "reason": "load average is not a number", "cores": cpu.get("cores")}
    disks = []
    if "disk_bsize" in kv or "disk_blocks" in kv:
        bsize, b_e = _num(kv.get("disk_bsize"))
        blocks, k_e = _num(kv.get("disk_blocks"))
        avail, a_e = _num(kv.get("disk_avail"))
        if b_e or k_e or a_e or not bsize or blocks is None or avail is None:
            disks = [_volume(None, None, "system", "disk size is not a number", "unavailable")]
        else:
            disks = [_volume(bsize * blocks, bsize * avail, "system")]
    bats = kv.get("bats")
    if bats is None:
        battery = {"applicability": "unreported", "reason": "No battery section in this reading", "supplies": []}
    elif bats in ("", "none"):
        battery = {"applicability": "not_applicable", "reason": "No battery reported.", "supplies": []}
    elif bats == "missing":
        battery = {"applicability": "unavailable", "reason": "power_supply is not readable.", "supplies": []}
    else:
        supplies = []
        for piece in str(bats).split(";"):
            if not piece:
                continue
            parts = piece.split(",")
            if len(parts) < 8:
                continue
            name, status, cap, efd, ef, cfd, cf, cyc = parts[:8]
            if not _NAME.fullmatch(name):
                continue
            supplies.append({"kind": "linux", "name": name, "status": status, "capacity": cap or None,
                             "energy_full_design": efd or None, "energy_full": ef or None,
                             "charge_full_design": cfd or None, "charge_full": cf or None,
                             "cycle_count": cyc or None})
            if len(supplies) >= MAX_SUPPLIES:
                break
        battery = ({"applicability": "present", "supplies": supplies} if supplies
                   else {"applicability": "not_applicable", "reason": "No battery reported.", "supplies": []})
    return {"v": 1, "measured_at": _iso(_now(now)), "source": "probe",
            "ram": ram, "swap": swap, "disks": disks, "cpu": cpu, "battery": battery}


def legacy_from_inventory(inv):
    """An inventory written before this reading existed. Uses only figures it actually stored."""
    if not isinstance(inv, dict) or not inv:
        return {"v": 1, "applicability": "unreported", "reason": "No inventory saved for this computer."}
    hw = inv.get("hardware") if isinstance(inv.get("hardware"), dict) else {}
    mem_text = hw.get("memory") if isinstance(hw.get("memory"), str) else ""
    info = _parse_meminfo(mem_text)
    if info.get("MemTotal") and info.get("MemAvailable"):
        ram = _mem_part(info["MemTotal"], info["MemAvailable"],
                        "Free counts memory the kernel can give out, including reclaimable cache.")
    else:
        ram = {"applicability": "unreported", "reason": "this inventory has no memory reading",
               "free_means": "Free counts memory the kernel can give out, including reclaimable cache."}
    if "SwapTotal" in info and info["SwapTotal"] == 0:
        swap = {"applicability": "not_applicable", "reason": "No swap is configured.", "free_means": "Swap free is unused swap."}
    elif info.get("SwapTotal") and "SwapFree" in info:
        swap = _mem_part(info["SwapTotal"], info["SwapFree"], "Swap free is unused swap.")
    else:
        swap = {"applicability": "unreported", "reason": "this inventory has no swap reading", "free_means": "Swap free is unused swap."}
    batteries = hw.get("batteries")
    if isinstance(batteries, list):
        supplies = []
        for entry in batteries[:MAX_SUPPLIES]:
            if isinstance(entry, dict):
                supplies.append({"kind": "linux", **{k: entry.get(k) for k in (
                    "name", "status", "capacity", "energy_full", "energy_full_design", "energy_now",
                    "charge_full", "charge_full_design", "charge_now", "cycle_count")}})
        battery = ({"applicability": "present", "supplies": supplies} if supplies
                   else {"applicability": "not_applicable", "reason": "No battery reported.", "supplies": []})
    elif isinstance(batteries, dict) and batteries.get("supplies"):
        battery = batteries
    else:
        battery = {"applicability": "unreported",
                   "reason": "This inventory has no battery section. Update the node and collect inventory again.",
                   "supplies": []}
    cpu = {"applicability": "unreported", "reason": "this inventory has no load reading"}
    cores = _legacy_cores(hw.get("cpu"))
    if cores:
        cpu["cores"] = cores
    return {"v": 1, "measured_at": inv.get("collected_at"), "source": "inventory",
            "ram": ram, "swap": swap, "disks": [], "cpu": cpu, "battery": battery}


def _legacy_cores(cpu):
    if isinstance(cpu, dict):
        number, reason = _num(cpu.get("NumberOfLogicalProcessors") or cpu.get("NumberOfCores"))
        if reason is None and number:
            return number
        for row in cpu.get("lscpu") or []:
            if isinstance(row, dict) and str(row.get("field") or "").startswith("CPU(s)"):
                number, reason = _num(row.get("data"))
                if reason is None:
                    return number
    return None


def accept(payload):
    """Heartbeat payload -> canonical reading, or None if it isn't one. Drops unknown keys."""
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError):
        return None
    if len(encoded) > 12000:
        return None
    source = payload.get("source") if payload.get("source") in ("sysfs", "cim", "probe", "inventory") else None
    if payload.get("applicability") in ("unreported", "unavailable") and "ram" not in payload:
        return {"v": 1, "measured_at": _safe(payload.get("measured_at")), "source": source,
                "applicability": payload["applicability"], "reason": _safe(payload.get("reason")) or "no reading"}
    # Re-run the section builders so a health percent in the payload is ignored.
    battery = payload.get("battery") if isinstance(payload.get("battery"), dict) else {}
    supplies = []
    for entry in (battery.get("supplies") or [])[:MAX_SUPPLIES]:
        if not isinstance(entry, dict):
            continue
        name = _supply_name(entry.get("name"), "Battery")
        windows = entry.get("kind") == "windows" or "win_full_mwh" in entry or "wmi_full_mwh" in entry
        if windows:
            item = {k: entry.get(k) for k in (
                "charge_percent", "status_code", "win_full_mwh", "win_design_mwh",
                "wmi_full_mwh", "wmi_design_mwh", "cycle_count")}
            item["kind"] = "windows"
            item["name"] = name
            if entry.get("power") in ("charging", "discharging", "full", "ac", "unknown", "mixed"):
                item["power"] = entry["power"]
            if entry.get("cycle_error") is True:
                item["cycle_error"] = True
        else:
            item = {k: entry.get(k) for k in (
                "capacity", "energy_full", "energy_full_design", "charge_full",
                "charge_full_design", "cycle_count")}
            item["kind"] = "linux"
            item["name"] = name
            item["status"] = _safe(entry.get("status"))
            errors = _clean_errors(entry.get("errors"))
            if errors:
                item["errors"] = errors
        supplies.append(item)
    kept = {
        "v": 1, "measured_at": _safe(payload.get("measured_at")), "source": source or "sysfs",
        "ram": _copy_bytes(payload.get("ram")), "swap": _copy_bytes(payload.get("swap")),
        "disks": [_copy_bytes(d) | {"name": _safe((d or {}).get("name")) or "disk"}
                  for d in (payload.get("disks") or [])[:MAX_DISKS] if isinstance(d, dict)],
        "cpu": _copy_cpu(payload.get("cpu")),
        "battery": {"applicability": battery.get("applicability") if battery.get("applicability") in (
            "present", "not_applicable", "unavailable", "unreported") else "unreported",
                    "reason": _safe(battery.get("reason")), "supplies": supplies},
    }
    return kept


def _copy_bytes(part):
    if not isinstance(part, dict):
        return {"applicability": "unreported", "reason": "not reported"}
    if part.get("applicability") in ("unavailable", "unreported", "not_applicable"):
        return {"applicability": part["applicability"], "reason": _safe(part.get("reason")),
                "free_means": _safe(part.get("free_means"))}
    total, _ = _num(part.get("total"))
    free, _ = _num(part.get("free"))
    if total is None or free is None:
        return {"applicability": "unavailable", "reason": "size not reported"}
    return {"total": total, "free": free, "unit": "B", "free_means": _safe(part.get("free_means"))}


def _copy_cpu(part):
    if not isinstance(part, dict):
        return {"applicability": "unreported", "reason": "no load reading"}
    if part.get("applicability") in ("unavailable", "unreported"):
        return {"applicability": part["applicability"], "reason": _safe(part.get("reason"))}
    load, load_e = _num(part.get("load1"))
    cores, cores_e = _num(part.get("cores"))
    busy, busy_e = _num(part.get("busy_percent"))
    out = {}
    if load_e is None:
        out["load1"] = load
    if cores_e is None:
        out["cores"] = cores
    if busy_e is None:
        out["busy_percent"] = busy
    if not out:
        return {"applicability": "unreported", "reason": "no load reading"}
    return out


def slim(raw):
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    out.pop("battery_rows", None)
    return out


def for_device(inv, node, *, local_sample=None, now=None, presence=None):
    """The view for one configured computer. Local sample wins, then a newer heartbeat, then inventory."""
    if isinstance(local_sample, dict):
        raw = local_sample
    else:
        node_raw = node.get("meters") if isinstance(node, dict) else None
        inv_raw = inv.get("meters") if isinstance(inv, dict) else None
        if not _canonical(node_raw):
            node_raw = None
        if not _canonical(inv_raw):
            inv_raw = None
        if node_raw and inv_raw:
            raw = node_raw if _measured(node_raw) >= _measured(inv_raw) else inv_raw
        else:
            raw = node_raw or inv_raw or legacy_from_inventory(inv if isinstance(inv, dict) else {})
    return view(raw, now=now, presence=presence)


def _canonical(raw):
    return isinstance(raw, dict) and raw.get("v") == 1


def _measured(raw):
    _, _, _ = (None, None, None)
    age, _fresh, _when = _stamp(raw.get("measured_at"), 0)
    # _stamp(now=0) returns now-at, so a later reading has a more negative age. Use the timestamp.
    try:
        text = str(raw.get("measured_at") or "").replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return 0


def summarize(device, inv, node, *, here=False, presence=None, now=None):
    """The view for one configured computer. Never raises."""
    try:
        sample = live_if_host(device) if here else None
        return for_device(inv if isinstance(inv, dict) else {},
                          node if isinstance(node, dict) else {},
                          local_sample=sample, now=now, presence=presence)
    except Exception as exc:
        return view({"v": 1, "applicability": "unavailable",
                     "reason": f"couldn't build the reading ({type(exc).__name__})"},
                    now=now, presence=presence)


def _copy_out(value):
    """A caller-owned copy. A cached exception propagates; the cache keeps its own."""
    if isinstance(value, Exception):
        raise value
    return copy.deepcopy(value)


def _copy_in(value):
    if isinstance(value, Exception):
        return value
    return copy.deepcopy(value)


class _FlightCache:
    """TTL cache with one in-flight call. Waiters share that result.

    `clock` is a monotonic clock, injectable so tests can expire an entry.
    A result for which `failed` is true is kept only for `fail_ttl`.
    """

    def __init__(self, ttl, fail_ttl=_FAIL_TTL):
        self.ttl = ttl
        self.fail_ttl = fail_ttl
        self._cond = threading.Condition()
        self._has = False
        self._ok = False
        self._at = 0.0
        self._value = None
        self._flight = False

    def clear(self):
        with self._cond:
            self._has = False
            self._value = None

    def _hit(self, clock):
        if not self._has:
            return False
        limit = self.ttl if self._ok else self.fail_ttl
        return clock() - self._at < limit

    def get(self, produce, *, clock=None, failed=None):
        clock = clock or time.monotonic
        failed = failed or (lambda _value: False)
        with self._cond:
            while True:
                if self._hit(clock):
                    return _copy_out(self._value)
                if not self._flight:
                    self._flight = True
                    break
                self._cond.wait()
        try:
            value = produce()
            bad = bool(failed(value))
            stored = _copy_in(value)
        except Exception as exc:
            stored, bad = exc, True
        with self._cond:
            self._value = stored
            self._ok = not bad
            self._has = True
            self._at = clock()
            self._flight = False
            self._cond.notify_all()
            return _copy_out(stored)


def _meter_failed(reading):
    """Whole-query failures only. One CIM class failing is still a reading."""
    if not isinstance(reading, dict):
        return True
    if reading.get("status") == "unavailable":
        return True
    return reading.get("applicability") == "unavailable" and "ram" not in reading


_meter_cache = _FlightCache(_SAMPLE_TTL, _FAIL_TTL)


def clear_cache():
    """Drop cached Windows meter readings. Tests call this between cases."""
    _meter_cache.clear()
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0
        _CACHE["raw"] = None


def _use_windows_meters():
    return os.name == "nt" or _is_windows()


def live_if_host(device):
    """This computer's reading when the device hostname is this hostname, else None."""
    host = socket.gethostname().split(".")[0].lower()
    want = str((device or {}).get("hostname") or "").split(".")[0].lower()
    if not host or not want or host != want:
        return None
    return cached_sample()


def cached_sample(ttl=_CACHE_TTL, *, clock=None):
    # Windows CIM is cached inside sample() (single-flight, _SAMPLE_TTL). A second
    # cache here would hold a failure longer than _FAIL_TTL. Linux keeps this one.
    if _use_windows_meters():
        return slim(sample(clock=clock))
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE["raw"] is not None and now - _CACHE["at"] < ttl:
            return _CACHE["raw"]
    raw = slim(sample())
    with _CACHE_LOCK:
        _CACHE["at"] = now
        _CACHE["raw"] = raw
    return raw


def sample(timeout=8, *, clock=None):
    """Read this computer. Never raises. Windows uses one bounded CIM query.

    That query is shared for _SAMPLE_TTL (a poll and the Devices page). A failure
    is kept only for _FAIL_TTL. Linux stays a direct read.
    """
    try:
        if _use_windows_meters():
            return _meter_cache.get(
                lambda: windows_canonical(_run_windows(timeout), _iso(time.time())),
                clock=clock, failed=_meter_failed)
        mounts = [("system", "/")]
        home = Path.home()
        try:
            if os.stat("/").st_dev != os.stat(home).st_dev:
                mounts.append(("home", home))
        except OSError:
            pass
        return read_linux(mounts=mounts)
    except Exception as exc:  # a meter must not take down inventory or a poll
        return {"v": 1, "measured_at": _iso(time.time()), "source": None,
                "applicability": "unavailable", "reason": f"couldn't read this computer ({type(exc).__name__})"}


def _is_windows():
    return __import__("platform").system() == "Windows"


def _run_windows(timeout):
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_METERS_SCRIPT],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            shell=False, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    if result.returncode:
        return {"status": "unavailable", "reason": "exit"}
    try:
        data = json.loads((result.stdout or "").strip().lstrip("\ufeff"))
    except ValueError:
        return {"status": "unavailable", "reason": "not_json"}
    return data if isinstance(data, dict) else {"status": "unavailable", "reason": "not_json"}
