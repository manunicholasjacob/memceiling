"""Derive the theoretical peak memory bandwidth, or say UNKNOWN.

The formula membw uses, and states in every record:

    peak_GB_s = data_rate_MT_s * total_data_width_bits / 8 / 1000

where total_data_width_bits is the sum of the DATA width (not the total
width, so ECC check bits are excluded) over the populated memory modules,
and data_rate_MT_s is the configured transfer rate in megatransfers per
second. GB here is decimal (10^9 bytes), which is the convention memory
vendors use when they quote a peak, so the fraction-of-theoretical figure
compares like with like.

Two worked examples, to show the formula is the ordinary one:

  * two DDR5-4800 modules, 64 data bits each
        4800 * 128 / 8 / 1000 = 76.8 GB/s
  * one LPDDR4X-4267 32-bit channel
        4267 * 32 / 8 / 1000 = 17.068 GB/s

membw never substitutes a number from a vendor datasheet or a lookup table
for a part it recognises. If the memory configuration cannot be read, the
peak is UNKNOWN and the achieved fraction is omitted, because a guessed
denominator is the exact defect this tool exists to remove.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field

FORMULA = "peak_GB_s = data_rate_MT_s * total_data_width_bits / 8 / 1000"

# Why GB is decimal here, recorded in output so nobody has to guess.
GB_DEFINITION = "GB = 10^9 bytes (decimal), matching how memory transfer rates are quoted"


@dataclass
class MemoryModule:
    """One populated memory module as a provenance source describes it."""

    data_width_bits: int | None = None
    total_width_bits: int | None = None
    data_rate_mt_s: int | None = None
    configured_rate_mt_s: int | None = None
    size_bytes: int | None = None
    mem_type: str | None = None
    locator: str | None = None

    def as_dict(self) -> dict:
        return {
            "locator": self.locator,
            "type": self.mem_type,
            "size_bytes": self.size_bytes,
            "data_width_bits": self.data_width_bits,
            "total_width_bits": self.total_width_bits,
            "rated_rate_mt_s": self.data_rate_mt_s,
            "configured_rate_mt_s": self.configured_rate_mt_s,
        }


@dataclass
class TheoreticalPeak:
    """The derived peak, the formula, its inputs, and what went unread."""

    peak_gb_s: float | None
    formula: str = FORMULA
    inputs: dict = field(default_factory=dict)
    source: str | None = None
    modules: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.peak_gb_s is not None

    def as_dict(self) -> dict:
        return {
            "peak_gb_s": (
                None if self.peak_gb_s is None else round(self.peak_gb_s, 4)
            ),
            "status": "KNOWN" if self.known else "UNKNOWN",
            "formula": self.formula,
            "gb_definition": GB_DEFINITION,
            "inputs": dict(self.inputs),
            "source": self.source,
            "modules": [m.as_dict() for m in self.modules],
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Pure parsers and the arithmetic
# ---------------------------------------------------------------------------


def _parse_size_to_bytes(text: str) -> int | None:
    if not text:
        return None
    m = re.match(r"^\s*([0-9]+)\s*([KMGT])B\s*$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2).upper()]
    return int(m.group(1)) * mult


def _parse_bits(text: str) -> int | None:
    if not text:
        return None
    m = re.match(r"^\s*([0-9]+)\s*bits?\s*$", text.strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_rate(text: str) -> int | None:
    """Parse '4800 MT/s' or '3200 MHz'.

    A value given in MHz is read as the clock, not the transfer rate, and is
    NOT doubled: membw refuses to infer a DDR multiplier it was not told,
    because getting that wrong is a factor-of-two error in the denominator.
    Such a value is returned with the flag in parse_dmidecode_memory's
    'rate_unit_was_mhz' so the caller can decline to use it.
    """
    if not text:
        return None
    m = re.match(r"^\s*([0-9]+)\s*(MT/s|MHz)\s*$", text.strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_dmidecode_memory(text: str) -> list[MemoryModule]:
    """Parse the 'Memory Device' blocks of `dmidecode -t memory` output.

    Unpopulated slots, which dmidecode reports with 'Size: No Module
    Installed', are skipped. Returns one MemoryModule per populated slot.
    """
    modules: list[MemoryModule] = []
    block: dict = {}
    in_device = False

    def flush():
        if not in_device or not block:
            return
        size_text = block.get("size", "")
        if "no module" in size_text.lower():
            return
        module = MemoryModule(
            data_width_bits=_parse_bits(block.get("data width", "")),
            total_width_bits=_parse_bits(block.get("total width", "")),
            data_rate_mt_s=_parse_rate(block.get("speed", "")),
            configured_rate_mt_s=_parse_rate(
                block.get("configured memory speed", "")
                or block.get("configured clock speed", "")
            ),
            size_bytes=_parse_size_to_bytes(size_text),
            mem_type=(block.get("type") or None),
            locator=(block.get("locator") or None),
        )
        if module.size_bytes or module.data_width_bits:
            modules.append(module)

    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not raw.startswith(("\t", " ")) or not stripped:
            flush()
            block = {}
            in_device = stripped.lower() == "memory device"
            continue
        if not in_device:
            continue
        key, sep, value = stripped.partition(":")
        if sep:
            block[key.strip().lower()] = value.strip()
    flush()
    return modules


def parse_memory_config_string(text: str) -> list[MemoryModule]:
    """Parse an explicit override of the form RATE:DATA_WIDTH_BITS:MODULES.

    For example '4267:32:1' for a single 32-bit LPDDR4X-4267 channel, or
    '4800:64:2' for two 64-bit DDR5-4800 modules. A fourth optional field
    names the memory type, as in '4800:64:2:DDR5'.

    Raises ValueError on anything it cannot parse, so a typo in a flag that
    sets the denominator fails loudly.
    """
    parts = [p.strip() for p in (text or "").split(":")]
    if len(parts) not in (3, 4):
        raise ValueError(
            "memory config must be RATE_MT_S:DATA_WIDTH_BITS:MODULES"
            " with an optional :TYPE, got %r" % (text,)
        )
    rate, width, count = int(parts[0]), int(parts[1]), int(parts[2])
    mem_type = parts[3] if len(parts) == 4 and parts[3] else None
    if rate <= 0 or width <= 0 or count <= 0:
        raise ValueError("memory config values must all be positive, got %r" % (text,))
    return [
        MemoryModule(
            data_width_bits=width,
            data_rate_mt_s=rate,
            configured_rate_mt_s=rate,
            mem_type=mem_type,
            locator="override-%d" % (i + 1),
        )
        for i in range(count)
    ]


def peak_from_modules(modules: list, source: str | None = None) -> TheoreticalPeak:
    """Apply FORMULA to a list of MemoryModule, or report UNKNOWN.

    The configured rate is preferred over the rated rate, because the
    configured rate is what the controller is actually running at. If the
    populated modules disagree on rate, the LOWEST is used, since a mixed
    population runs at the slowest module's rate, and the disagreement is
    recorded in the note.
    """
    unreadable: list[str] = []
    usable = [m for m in modules if m.data_width_bits]
    if not modules:
        return TheoreticalPeak(
            peak_gb_s=None,
            source=source,
            unreadable=["memory module inventory"],
            note="No populated memory module was described by any readable source.",
        )
    if not usable:
        unreadable.append("memory module data width")

    rates = []
    for m in modules:
        rate = m.configured_rate_mt_s or m.data_rate_mt_s
        if rate:
            rates.append(rate)
    if not rates:
        unreadable.append("memory transfer rate (MT/s)")

    if not usable or not rates:
        return TheoreticalPeak(
            peak_gb_s=None,
            source=source,
            modules=list(modules),
            unreadable=unreadable,
            note=(
                "Theoretical peak is UNKNOWN: the formula needs both a data "
                "width and a transfer rate, and at least one was not readable."
            ),
        )

    total_width = sum(int(m.data_width_bits) for m in usable)
    rate = min(rates)
    peak = rate * total_width / 8.0 / 1000.0

    note = None
    if len(set(rates)) > 1:
        note = (
            "Populated modules reported different transfer rates %s MT/s; the "
            "lowest was used, because a mixed population runs at the slowest "
            "module's rate." % (sorted(set(rates)),)
        )
    if len(usable) != len(modules):
        extra = (
            "%d of %d populated modules had no readable data width and were "
            "excluded from total_data_width_bits, so this peak is a lower "
            "bound." % (len(modules) - len(usable), len(modules))
        )
        note = extra if note is None else note + " " + extra

    return TheoreticalPeak(
        peak_gb_s=peak,
        source=source,
        modules=list(modules),
        inputs={
            "data_rate_mt_s": rate,
            "total_data_width_bits": total_width,
            "module_count": len(usable),
            "per_module_data_width_bits": [int(m.data_width_bits) for m in usable],
            "memory_types": sorted({m.mem_type for m in modules if m.mem_type}),
        },
        unreadable=unreadable,
        note=note,
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def read_dmidecode_modules() -> tuple[list, str | None, list]:
    """Run `dmidecode -t memory` if it is present and permitted."""
    unreadable: list[str] = []
    exe = shutil.which("dmidecode")
    if not exe:
        unreadable.append("dmidecode (not installed)")
        return [], None, unreadable
    try:
        proc = subprocess.run(
            [exe, "-t", "memory"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        unreadable.append("dmidecode (could not run)")
        return [], None, unreadable
    if proc.returncode != 0 or not proc.stdout.strip():
        euid = getattr(os, "geteuid", lambda: -1)()
        detail = "permission denied or unreadable" if euid != 0 else "no output"
        unreadable.append("dmidecode -t memory (%s)" % detail)
        return [], None, unreadable
    modules = parse_dmidecode_memory(proc.stdout)
    if not modules:
        unreadable.append("dmidecode -t memory (no populated Memory Device block)")
        return [], None, unreadable
    return modules, "dmidecode -t memory (SMBIOS)", unreadable


def read_windows_modules() -> tuple[list, str | None, list]:
    """Read Win32_PhysicalMemory through PowerShell CIM.

    This is the Windows equivalent of reading SMBIOS and needs no
    administrator rights.
    """
    unreadable: list[str] = []
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        unreadable.append("PowerShell (not found, cannot read Win32_PhysicalMemory)")
        return [], None, unreadable
    script = (
        "Get-CimInstance Win32_PhysicalMemory | "
        "Select-Object DeviceLocator,Capacity,Speed,ConfiguredClockSpeed,"
        "DataWidth,TotalWidth,SMBIOSMemoryType | ConvertTo-Json -Compress -Depth 3"
    )
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        unreadable.append("Win32_PhysicalMemory (could not run PowerShell)")
        return [], None, unreadable
    if proc.returncode != 0 or not proc.stdout.strip():
        unreadable.append("Win32_PhysicalMemory (no output)")
        return [], None, unreadable
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        unreadable.append("Win32_PhysicalMemory (output was not JSON)")
        return [], None, unreadable
    if isinstance(data, dict):
        data = [data]
    modules = []
    for row in data:
        if not isinstance(row, dict):
            continue
        modules.append(
            MemoryModule(
                data_width_bits=row.get("DataWidth"),
                total_width_bits=row.get("TotalWidth"),
                data_rate_mt_s=row.get("Speed"),
                configured_rate_mt_s=row.get("ConfiguredClockSpeed"),
                size_bytes=int(row["Capacity"]) if row.get("Capacity") else None,
                mem_type=SMBIOS_MEMORY_TYPES.get(row.get("SMBIOSMemoryType")),
                locator=row.get("DeviceLocator"),
            )
        )
    if not modules:
        unreadable.append("Win32_PhysicalMemory (no rows)")
        return [], None, unreadable
    return modules, "Win32_PhysicalMemory (SMBIOS via CIM)", unreadable


# SMBIOS memory device type codes, so the type field is a read value rather
# than a guess. Only the codes membw may plausibly meet are listed.
SMBIOS_MEMORY_TYPES = {
    20: "DDR",
    21: "DDR2",
    24: "DDR3",
    26: "DDR4",
    34: "DDR5",
    35: "LPDDR5",
    30: "LPDDR",
    31: "LPDDR2",
    32: "LPDDR3",
    33: "LPDDR4",
}


def read_devicetree_modules() -> tuple[list, str | None, list]:
    """Look for a memory transfer rate in the Arm device tree.

    Most device trees, including the Raspberry Pi's, publish the memory
    size and nothing about the transfer rate or bus width. membw therefore
    usually reports UNKNOWN here and says which nodes it looked at, rather
    than filling in a datasheet figure for the detected board.
    """
    roots = ("/proc/device-tree", "/sys/firmware/devicetree/base")
    root = next((r for r in roots if os.path.isdir(r)), None)
    if root is None:
        return [], None, ["Arm device tree (/proc/device-tree not present)"]

    checked = []
    rate = None
    width = None
    for node in ("memory-controller", "memory", "dram", "lpddr"):
        for candidate in (
            os.path.join(root, node),
            os.path.join(root, node + "@0"),
        ):
            checked.append(candidate)
            if not os.path.isdir(candidate):
                continue
            for prop, target in (
                ("clock-frequency", "rate"),
                ("max-freq", "rate"),
                ("bus-width", "width"),
            ):
                path = os.path.join(candidate, prop)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, "rb") as handle:
                        blob = handle.read(4)
                except OSError:
                    continue
                if len(blob) != 4:
                    continue
                value = int.from_bytes(blob, "big")
                if target == "rate" and value:
                    # A clock frequency is not a transfer rate. membw does
                    # not double it, so it is recorded and not used.
                    rate = rate or value
                elif target == "width" and value:
                    width = width or value

    unreadable = [
        "Arm device tree memory transfer rate (looked at %s)" % ", ".join(checked[:4])
    ]
    if rate is not None:
        unreadable.append(
            "Arm device tree gave a clock frequency of %d Hz but no transfer "
            "rate; membw does not infer a DDR multiplier" % rate
        )
    return [], None, unreadable


def derive_theoretical_peak(memory_config: str | None = None) -> TheoreticalPeak:
    """Derive the theoretical peak from the best readable source.

    memory_config, if given, is an explicit RATE:WIDTH:MODULES override and
    takes precedence over everything else. It is recorded as an override in
    the output so a consumer can tell a read figure from a supplied one.
    """
    if memory_config:
        modules = parse_memory_config_string(memory_config)
        peak = peak_from_modules(modules, source="explicit --memory-config override")
        peak.note = (
            "Theoretical peak came from an operator-supplied memory "
            "configuration, not from a reading of this machine."
            + (" " + peak.note if peak.note else "")
        )
        return peak

    env_override = os.environ.get("MEMBW_MEMORY_CONFIG")
    if env_override:
        modules = parse_memory_config_string(env_override)
        peak = peak_from_modules(
            modules, source="MEMBW_MEMORY_CONFIG environment override"
        )
        peak.note = (
            "Theoretical peak came from the MEMBW_MEMORY_CONFIG environment "
            "variable, not from a reading of this machine."
            + (" " + peak.note if peak.note else "")
        )
        return peak

    unreadable: list[str] = []
    system = platform.system()
    readers = []
    if system == "Windows":
        readers = [read_windows_modules]
    else:
        readers = [read_dmidecode_modules, read_devicetree_modules]

    for reader in readers:
        modules, source, reader_unreadable = reader()
        unreadable.extend(reader_unreadable)
        if modules:
            peak = peak_from_modules(modules, source=source)
            peak.unreadable = unreadable + list(peak.unreadable)
            return peak

    return TheoreticalPeak(
        peak_gb_s=None,
        source=None,
        unreadable=unreadable,
        note=(
            "Theoretical peak is UNKNOWN. No readable source gave both a "
            "memory transfer rate and a data width. Supply one with "
            "--memory-config RATE_MT_S:DATA_WIDTH_BITS:MODULES if you know "
            "the configuration; it will be recorded as an override."
        ),
    )
