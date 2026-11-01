"""CPU topology, cache sizes and memory capacity, with the source recorded.

Every reader in this module either returns a value and the name of the file
or API it came from, or returns None and appends to an "unreadable" list.
That list reaches the output envelope so a consumer can tell the difference
between a clean machine and an unread one.

The parsing is deliberately separated from the reading: the parse_* and
group_* functions are pure, take text or plain dicts, and are what the test
suite exercises. The read_* functions are thin and do nothing but fetch
bytes and hand them to a parser.
"""

from __future__ import annotations

import ctypes
import glob
import os
import platform
import re
import sys
from dataclasses import dataclass, field

# Arm MIDR primary part numbers. This is a lookup of published part
# identifiers, used only to give a cluster a human-readable name; cluster
# membership itself is derived from the implementer/part pair, not from this
# table, so an unknown part still clusters correctly.
ARM_PART_NAMES = {
    0xD01: "Cortex-A32",
    0xD03: "Cortex-A53",
    0xD04: "Cortex-A35",
    0xD05: "Cortex-A55",
    0xD07: "Cortex-A57",
    0xD08: "Cortex-A72",
    0xD09: "Cortex-A73",
    0xD0A: "Cortex-A75",
    0xD0B: "Cortex-A76",
    0xD0C: "Neoverse-N1",
    0xD0D: "Cortex-A77",
    0xD40: "Neoverse-V1",
    0xD41: "Cortex-A78",
    0xD44: "Cortex-X1",
    0xD46: "Cortex-A510",
    0xD47: "Cortex-A710",
    0xD48: "Cortex-X2",
    0xD49: "Neoverse-N2",
    0xD4A: "Neoverse-E1",
    0xD4B: "Cortex-A78C",
    0xD4D: "Cortex-A715",
    0xD4E: "Cortex-X3",
    0xD80: "Cortex-A520",
    0xD81: "Cortex-A720",
    0xD82: "Cortex-X4",
}

CACHE_TYPE_UNIFIED = "Unified"
CACHE_TYPE_DATA = "Data"
CACHE_TYPE_INSTRUCTION = "Instruction"


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def parse_cpu_list(text: str) -> tuple[int, ...]:
    """Parse a Linux CPU list such as '0-3,8,10-11' into a sorted tuple.

    An empty or whitespace-only string yields an empty tuple.
    """
    out: set[int] = set()
    for chunk in (text or "").strip().split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            out.add(int(chunk))
    return tuple(sorted(out))


def format_cpu_list(cpus) -> str:
    """Render a collection of CPU ids as a compact Linux-style CPU list."""
    ordered = sorted(set(int(c) for c in cpus))
    if not ordered:
        return ""
    runs = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        runs.append((start, prev))
        start = prev = cpu
    runs.append((start, prev))
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in runs)


def parse_cache_size(text: str) -> int | None:
    """Parse a cache size as sysfs and dmidecode write them, into bytes.

    Accepts '32K', '8192K', '4M', '32 KiB', '1.5 MB', '262144'. Returns None
    if nothing numeric can be found. Suffixed values use binary multipliers,
    which is what both sysfs and dmidecode mean by K and M for cache sizes.
    """
    if text is None:
        return None
    s = str(text).strip()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGkmg])?(?:i?[Bb])?$", s)
    if not m:
        return None
    value = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[suffix]
    return int(value * mult)


def parse_cpuinfo_arm_clusters(text: str) -> list[dict]:
    """Group CPUs by Arm implementer/part from /proc/cpuinfo text.

    Returns a list of dicts with keys 'implementer', 'part', 'variant',
    'name' and 'cpus', ordered by the lowest CPU id in each group. Returns an
    empty list if the text carries no Arm part information, which is the case
    on x86 and is not an error.

    The grouping key is (implementer, part). Variant and revision are
    recorded but deliberately not part of the key: a stepping difference is
    not a different cluster.
    """
    blocks: list[dict] = []
    current: dict = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        current[key.strip().lower()] = value.strip()
    if current:
        blocks.append(current)

    groups: dict[tuple, dict] = {}
    for block in blocks:
        if "processor" not in block or "cpu part" not in block:
            continue
        try:
            cpu = int(block["processor"])
            part = int(block["cpu part"], 16)
        except ValueError:
            continue
        impl_raw = block.get("cpu implementer")
        try:
            implementer = int(impl_raw, 16) if impl_raw else None
        except ValueError:
            implementer = None
        variant_raw = block.get("cpu variant")
        try:
            variant = int(variant_raw, 16) if variant_raw else None
        except ValueError:
            variant = None
        key = (implementer, part)
        group = groups.setdefault(
            key,
            {
                "implementer": implementer,
                "part": part,
                "variant": variant,
                "name": ARM_PART_NAMES.get(part, "part-0x%03x" % part),
                "cpus": [],
            },
        )
        group["cpus"].append(cpu)

    result = []
    for group in groups.values():
        group["cpus"] = tuple(sorted(group["cpus"]))
        result.append(group)
    result.sort(key=lambda g: g["cpus"][0] if g["cpus"] else -1)
    return result


def group_by_capacity(capacities: dict) -> list[dict]:
    """Group CPUs by their sysfs cpu_capacity value.

    capacities maps cpu id to integer capacity. CPUs with equal capacity are
    one cluster. Returns groups ordered by ascending capacity, so the
    smallest cores come first, each with keys 'capacity', 'name' and 'cpus'.
    A single distinct capacity yields a single group, which callers treat as
    "not heterogeneous".
    """
    buckets: dict[int, list[int]] = {}
    for cpu, cap in capacities.items():
        buckets.setdefault(int(cap), []).append(int(cpu))
    groups = []
    for cap in sorted(buckets):
        groups.append(
            {
                "capacity": cap,
                "name": "capacity-%d" % cap,
                "cpus": tuple(sorted(buckets[cap])),
            }
        )
    return groups


def select_llc(entries: list[dict], selected_cpus=None) -> dict:
    """Pick the last-level cache from a list of cache descriptions.

    Each entry is a dict with 'level' (int), 'size_bytes' (int), 'type'
    (str) and 'shared_cpus' (iterable of int). The last level is the highest
    level at which a Unified or Data cache exists; instruction caches are
    ignored because they carry none of the benchmark's working set.

    Returns a dict with 'level', 'total_bytes', 'instances' and
    'instance_bytes'. 'total_bytes' is the sum over the DISTINCT cache
    instances whose shared_cpus intersect selected_cpus, which is the
    quantity STREAM's run rules refer to as "the sum of all the last-level
    caches used in the run". Returns an empty dict when there is nothing to
    pick.
    """
    usable = [
        e
        for e in entries
        if e.get("type") in (CACHE_TYPE_UNIFIED, CACHE_TYPE_DATA)
        and e.get("size_bytes")
        and e.get("level")
    ]
    if not usable:
        return {}
    level = max(int(e["level"]) for e in usable)
    at_level = [e for e in usable if int(e["level"]) == level]

    want = None if selected_cpus is None else set(int(c) for c in selected_cpus)
    seen: dict[tuple, int] = {}
    for entry in at_level:
        shared = tuple(sorted(set(int(c) for c in entry.get("shared_cpus") or ())))
        if want is not None and shared and not (set(shared) & want):
            continue
        # Key on the shared set so two reports of the same physical cache
        # (one per CPU that shares it) are counted once.
        seen[shared or (id(entry),)] = int(entry["size_bytes"])
    if not seen:
        return {}
    return {
        "level": level,
        "total_bytes": sum(seen.values()),
        "instances": len(seen),
        "instance_bytes": sorted(seen.values()),
    }


def parse_meminfo(text: str) -> dict:
    """Parse /proc/meminfo into a dict of byte counts.

    Only the keys membw uses are converted: MemTotal, MemAvailable, MemFree,
    SwapTotal, Hugepagesize. Values in meminfo are in kibibytes.
    """
    wanted = {
        "MemTotal": "total_bytes",
        "MemAvailable": "available_bytes",
        "MemFree": "free_bytes",
        "SwapTotal": "swap_total_bytes",
        "Hugepagesize": "hugepage_bytes",
    }
    out: dict = {}
    for raw in (text or "").splitlines():
        key, sep, rest = raw.partition(":")
        if not sep or key.strip() not in wanted:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        unit = parts[1].lower() if len(parts) > 1 else "kb"
        mult = 1024 if unit in ("kb", "kib") else 1
        out[wanted[key.strip()]] = value * mult
    return out


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    """One group of logical CPUs that membw will measure on its own."""

    name: str
    cpus: tuple[int, ...]
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "cpus": list(self.cpus),
            "cpu_list": format_cpu_list(self.cpus),
            "cpu_count": len(self.cpus),
            "detail": dict(self.detail),
        }


@dataclass
class Topology:
    """What membw managed to learn about the CPUs and memory capacity."""

    online_cpus: tuple[int, ...]
    clusters: tuple[Cluster, ...]
    cluster_source: str
    llc_level: int | None = None
    llc_total_bytes: int | None = None
    llc_instances: int | None = None
    llc_source: str | None = None
    total_ram_bytes: int | None = None
    available_ram_bytes: int | None = None
    ram_source: str | None = None
    cpu_model: str | None = None
    unreadable: list = field(default_factory=list)

    @property
    def heterogeneous(self) -> bool:
        return len(self.clusters) > 1

    def as_dict(self) -> dict:
        return {
            "cpu_model": self.cpu_model,
            "online_cpus": format_cpu_list(self.online_cpus),
            "online_cpu_count": len(self.online_cpus),
            "heterogeneous": self.heterogeneous,
            "cluster_source": self.cluster_source,
            "clusters": [c.as_dict() for c in self.clusters],
            "llc": {
                "level": self.llc_level,
                "total_bytes": self.llc_total_bytes,
                "instances": self.llc_instances,
                "source": self.llc_source,
            },
            "ram": {
                "total_bytes": self.total_ram_bytes,
                "available_bytes": self.available_ram_bytes,
                "source": self.ram_source,
            },
        }


# ---------------------------------------------------------------------------
# Thin readers
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_linux_cache_entries() -> list[dict]:
    entries = []
    for index_dir in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cache/index*")):
        level = _read_text(os.path.join(index_dir, "level"))
        size = _read_text(os.path.join(index_dir, "size"))
        ctype = _read_text(os.path.join(index_dir, "type"))
        shared = _read_text(os.path.join(index_dir, "shared_cpu_list"))
        if not (level and size and ctype):
            continue
        size_bytes = parse_cache_size(size.strip())
        if size_bytes is None:
            continue
        try:
            level_i = int(level.strip())
        except ValueError:
            continue
        entries.append(
            {
                "level": level_i,
                "size_bytes": size_bytes,
                "type": ctype.strip(),
                "shared_cpus": parse_cpu_list(shared or ""),
            }
        )
    return entries


def _windows_logical_processor_info():
    """Return (cache_entries, core_groups) from GetLogicalProcessorInformationEx.

    core_groups is a list of (efficiency_class, cpu_id) pairs. Returns
    (None, None) if the call cannot be made or parsed.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        return None, None

    relation_all = 0xFFFF
    length = ctypes.c_ulong(0)
    kernel32.GetLogicalProcessorInformationEx(relation_all, None, ctypes.byref(length))
    if length.value == 0:
        return None, None
    buf = (ctypes.c_ubyte * length.value)()
    ok = kernel32.GetLogicalProcessorInformationEx(
        relation_all, buf, ctypes.byref(length)
    )
    if not ok:
        return None, None

    raw = bytes(buf)
    cache_entries: list[dict] = []
    core_groups: list[tuple] = []
    cache_type_names = {
        0: CACHE_TYPE_UNIFIED,
        1: CACHE_TYPE_INSTRUCTION,
        2: CACHE_TYPE_DATA,
        3: "Trace",
    }
    rel_processor_core, rel_cache = 0, 2
    ptr_size = ctypes.sizeof(ctypes.c_void_p)
    group_affinity_size = ptr_size + 8  # KAFFINITY + WORD Group + WORD[3]

    def group_masks(blob, offset, count):
        out = []
        for i in range(count):
            base = offset + i * group_affinity_size
            if base + group_affinity_size > len(blob):
                break
            mask = int.from_bytes(blob[base : base + ptr_size], sys.byteorder)
            group = int.from_bytes(blob[base + ptr_size : base + ptr_size + 2], sys.byteorder)
            out.append((group, mask))
        return out

    def mask_to_cpus(group, mask):
        bits = ptr_size * 8
        return [group * 64 + b for b in range(bits) if mask & (1 << b)]

    pos = 0
    while pos + 8 <= len(raw):
        relationship = int.from_bytes(raw[pos : pos + 4], sys.byteorder)
        size = int.from_bytes(raw[pos + 4 : pos + 8], sys.byteorder)
        if size < 8 or pos + size > len(raw):
            break
        payload_at = pos + 8
        if relationship == rel_cache:
            level = raw[payload_at]
            cache_size = int.from_bytes(
                raw[payload_at + 4 : payload_at + 8], sys.byteorder
            )
            ctype = int.from_bytes(raw[payload_at + 8 : payload_at + 12], sys.byteorder)
            group_count = int.from_bytes(
                raw[payload_at + 12 : payload_at + 14], sys.byteorder
            ) or 1
            masks = group_masks(raw, payload_at + 24, group_count)
            cpus: list[int] = []
            for group, mask in masks:
                cpus.extend(mask_to_cpus(group, mask))
            if cache_size:
                cache_entries.append(
                    {
                        "level": level,
                        "size_bytes": cache_size,
                        "type": cache_type_names.get(ctype, "Unknown"),
                        "shared_cpus": tuple(sorted(cpus)),
                    }
                )
        elif relationship == rel_processor_core:
            efficiency_class = raw[payload_at + 1]
            group_count = int.from_bytes(
                raw[payload_at + 22 : payload_at + 24], sys.byteorder
            ) or 1
            masks = group_masks(raw, payload_at + 24, group_count)
            for group, mask in masks:
                for cpu in mask_to_cpus(group, mask):
                    core_groups.append((efficiency_class, cpu))
        pos += size

    return cache_entries, core_groups


def _windows_memory_status():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        return None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes": int(status.ullAvailPhys),
    }


def _linux_cpu_model(cpuinfo_text: str | None) -> str | None:
    if not cpuinfo_text:
        return None
    for raw in cpuinfo_text.splitlines():
        key, sep, value = raw.partition(":")
        if sep and key.strip().lower() in ("model name", "hardware", "processor name"):
            text = value.strip()
            if text:
                return text
    return None


def detect_topology(selected_cpus=None) -> Topology:
    """Read what can be read about CPUs, clusters, LLC and memory capacity.

    selected_cpus narrows the LLC sum to the caches reachable from those
    CPUs, which is what matters when membw is pinned to one cluster.
    """
    unreadable: list[str] = []
    system = platform.system()

    # Online CPUs.
    online = None
    if hasattr(os, "sched_getaffinity"):
        try:
            online = tuple(sorted(os.sched_getaffinity(0)))
        except OSError:
            online = None
    if online is None:
        online_text = _read_text("/sys/devices/system/cpu/online")
        if online_text:
            online = parse_cpu_list(online_text)
    if not online:
        count = os.cpu_count() or 1
        online = tuple(range(count))

    cpuinfo_text = _read_text("/proc/cpuinfo")
    cpu_model = _linux_cpu_model(cpuinfo_text)

    # Clusters.
    clusters: list[Cluster] = []
    cluster_source = "assumed-single-cluster"

    arm_groups = parse_cpuinfo_arm_clusters(cpuinfo_text or "")
    if len(arm_groups) > 1:
        cluster_source = "/proc/cpuinfo CPU implementer and CPU part"
        for group in arm_groups:
            clusters.append(
                Cluster(
                    name=group["name"],
                    cpus=group["cpus"],
                    detail={
                        "arm_implementer": group["implementer"],
                        "arm_part": group["part"],
                        "arm_variant": group["variant"],
                    },
                )
            )
    if not clusters:
        capacities = {}
        for path in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpu_capacity")):
            m = re.search(r"cpu(\d+)/cpu_capacity$", path.replace("\\", "/"))
            text = _read_text(path)
            if not (m and text):
                continue
            try:
                capacities[int(m.group(1))] = int(text.strip())
            except ValueError:
                continue
        groups = group_by_capacity(capacities) if capacities else []
        if len(groups) > 1:
            cluster_source = "/sys/devices/system/cpu/cpu*/cpu_capacity"
            for group in groups:
                clusters.append(
                    Cluster(
                        name=group["name"],
                        cpus=group["cpus"],
                        detail={"cpu_capacity": group["capacity"]},
                    )
                )

    cache_entries: list[dict] = []
    if system == "Windows":
        cache_entries_win, core_groups = _windows_logical_processor_info()
        if cache_entries_win is None:
            unreadable.append("GetLogicalProcessorInformationEx")
        else:
            cache_entries = cache_entries_win
            if not clusters and core_groups:
                buckets: dict[int, list[int]] = {}
                for efficiency_class, cpu in core_groups:
                    buckets.setdefault(efficiency_class, []).append(cpu)
                if len(buckets) > 1:
                    cluster_source = (
                        "GetLogicalProcessorInformationEx "
                        "PROCESSOR_RELATIONSHIP.EfficiencyClass"
                    )
                    for efficiency_class in sorted(buckets):
                        clusters.append(
                            Cluster(
                                name="efficiency-class-%d" % efficiency_class,
                                cpus=tuple(sorted(set(buckets[efficiency_class]))),
                                detail={"efficiency_class": efficiency_class},
                            )
                        )
    else:
        cache_entries = _read_linux_cache_entries()
        if not cache_entries:
            unreadable.append("/sys/devices/system/cpu/cpu*/cache/index*")

    if not clusters:
        clusters.append(Cluster(name="all", cpus=tuple(online), detail={}))

    llc = select_llc(cache_entries, selected_cpus) if cache_entries else {}
    if llc:
        llc_source = (
            "GetLogicalProcessorInformationEx RelationCache"
            if system == "Windows"
            else "/sys/devices/system/cpu/cpu*/cache/index*"
        )
    else:
        llc_source = None
        unreadable.append("last-level cache size")

    # Memory capacity.
    ram = {}
    ram_source = None
    meminfo_text = _read_text("/proc/meminfo")
    if meminfo_text:
        ram = parse_meminfo(meminfo_text)
        ram_source = "/proc/meminfo"
    elif system == "Windows":
        win_ram = _windows_memory_status()
        if win_ram:
            ram = win_ram
            ram_source = "GlobalMemoryStatusEx"
    if not ram:
        unreadable.append("/proc/meminfo")

    if cpu_model is None and system == "Windows":
        cpu_model = os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor() or None
        if cpu_model is None:
            unreadable.append("CPU model name")

    return Topology(
        online_cpus=tuple(online),
        clusters=tuple(clusters),
        cluster_source=cluster_source,
        llc_level=llc.get("level"),
        llc_total_bytes=llc.get("total_bytes"),
        llc_instances=llc.get("instances"),
        llc_source=llc_source,
        total_ram_bytes=ram.get("total_bytes"),
        available_ram_bytes=ram.get("available_bytes", ram.get("free_bytes")),
        ram_source=ram_source,
        cpu_model=cpu_model,
        unreadable=unreadable,
    )
