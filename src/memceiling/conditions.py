"""Read the run conditions that decide whether a bandwidth number is valid.

Three groups of things are read here:

  1. State that must not change across the run: the CPU frequency governor.
  2. Counters that must not advance during the run: thermal throttle counts,
     cgroup CPU quota throttling, swap activity, major page faults.
  3. A pre-run observation: how busy the target CPUs already are.

Everything is read twice, before and after, except the competing-load
observation which is taken before the run so that memceiling's own load does not
appear in it. Any source that cannot be read is named, because the whole
point is that a consumer can distinguish "nothing was wrong" from "nothing
was checked".

memceiling never changes the governor unless explicitly told to with
set_governor(), and when it does, it restores the previous value and reads
it back to confirm.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def parse_proc_stat(text: str) -> dict:
    """Parse per-CPU lines of /proc/stat into {cpu_id: [jiffies, ...]}.

    The aggregate 'cpu' line is returned under the key -1.
    """
    out: dict = {}
    for raw in (text or "").splitlines():
        if not raw.startswith("cpu"):
            continue
        parts = raw.split()
        if len(parts) < 5:
            continue
        label = parts[0]
        if label == "cpu":
            key = -1
        else:
            try:
                key = int(label[3:])
            except ValueError:
                continue
        try:
            out[key] = [int(p) for p in parts[1:]]
        except ValueError:
            continue
    return out


def busy_fraction_from_proc_stat(before: dict, after: dict, cpus=None):
    """Busy fraction over the interval between two parse_proc_stat results.

    Field order in /proc/stat is user, nice, system, idle, iowait, irq,
    softirq, steal, guest, guest_nice. Idle time is idle plus iowait; a CPU
    waiting on I/O is not doing memory traffic. Guest and guest_nice are
    already counted inside user and nice, so they are dropped to avoid
    double counting.

    Returns None if the two snapshots do not cover any of the requested CPUs
    or no time elapsed.
    """
    keys = (
        [k for k in before if k >= 0]
        if cpus is None
        else [int(c) for c in cpus if int(c) in before]
    )
    total_delta = 0
    idle_delta = 0
    for key in keys:
        if key not in after:
            continue
        a, b = before[key], after[key]
        width = min(len(a), len(b), 8)
        deltas = [b[i] - a[i] for i in range(width)]
        if any(d < 0 for d in deltas):
            return None
        total_delta += sum(deltas)
        idle_delta += sum(deltas[3:5])
    if total_delta <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - idle_delta / float(total_delta)))


def parse_cgroup_cpu_stat(text: str) -> dict:
    """Parse cgroup v2 cpu.stat into a dict of integer counters."""
    out: dict = {}
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


def parse_vmstat(text: str) -> dict:
    """Parse /proc/vmstat, keeping only the swap counters memceiling uses."""
    wanted = ("pswpin", "pswpout")
    out: dict = {}
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) == 2 and parts[0] in wanted:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out


def parse_proc_self_stat_majflt(text: str):
    """Extract the major fault count (field 12) from /proc/self/stat.

    The command name in field 2 is parenthesised and may itself contain
    spaces and parentheses, so the text after the LAST ')' is split.
    """
    if not text:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1 :].split()
    # After the ')' the next field is state (field 3), so field 12 (majflt)
    # is at index 12 - 3 = 9.
    if len(fields) <= 9:
        return None
    try:
        return int(fields[9])
    except ValueError:
        return None


def parse_vcgencmd_throttled(text: str):
    """Parse `vcgencmd get_throttled` output, as on a Raspberry Pi.

    Output looks like 'throttled=0x50005'. Returns a dict with the raw
    value and the decoded flags, or None if the text does not match.
    """
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", text or "")
    if not m:
        return None
    value = int(m.group(1), 16)
    return {
        "raw": value,
        "under_voltage_now": bool(value & (1 << 0)),
        "arm_frequency_capped_now": bool(value & (1 << 1)),
        "currently_throttled": bool(value & (1 << 2)),
        "soft_temperature_limit_now": bool(value & (1 << 3)),
        "under_voltage_occurred": bool(value & (1 << 16)),
        "arm_frequency_capped_occurred": bool(value & (1 << 17)),
        "throttling_occurred": bool(value & (1 << 18)),
        "soft_temperature_limit_occurred": bool(value & (1 << 19)),
    }


def compare_snapshots(before: "ConditionSnapshot", after: "ConditionSnapshot") -> dict:
    """Diff two snapshots into the facts the validity rules act on."""
    result: dict = {
        "governor_before": before.governor,
        "governor_after": after.governor,
        "governor_changed": (
            before.governor is not None
            and after.governor is not None
            and before.governor != after.governor
        ),
        "throttle_deltas": {},
        "throttled_during_run": {"thermal": False, "quota": False},
        "swap_delta": None,
        "major_fault_delta": None,
        "temperature_before_c": before.temperature_c,
        "temperature_after_c": after.temperature_c,
    }

    for name, value in (after.throttle_counters or {}).items():
        start = (before.throttle_counters or {}).get(name)
        if start is None or value is None:
            continue
        delta = value - start
        result["throttle_deltas"][name] = delta
        if delta > 0:
            if name.startswith("quota:"):
                result["throttled_during_run"]["quota"] = True
            else:
                result["throttled_during_run"]["thermal"] = True

    if before.swap and after.swap:
        delta = sum(after.swap.get(k, 0) - before.swap.get(k, 0) for k in after.swap)
        result["swap_delta"] = delta

    if before.major_faults is not None and after.major_faults is not None:
        result["major_fault_delta"] = after.major_faults - before.major_faults

    return result


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class ConditionSnapshot:
    """One reading of the mutable run conditions."""

    taken_at: float
    governor: str | None = None
    governor_per_cpu: dict = field(default_factory=dict)
    governor_source: str | None = None
    throttle_counters: dict = field(default_factory=dict)
    throttle_sources: list = field(default_factory=list)
    swap: dict = field(default_factory=dict)
    major_faults: int | None = None
    temperature_c: float | None = None
    power_plan: str | None = None
    unreadable: list = field(default_factory=list)


def _read_text(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_governor(cpus=None):
    per_cpu: dict = {}
    for path in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")):
        m = re.search(r"cpu(\d+)/cpufreq/scaling_governor$", path.replace("\\", "/"))
        text = _read_text(path)
        if not (m and text):
            continue
        per_cpu[int(m.group(1))] = text.strip()
    if not per_cpu:
        return None, {}, None
    if cpus:
        selected = {c: per_cpu[c] for c in (int(x) for x in cpus) if c in per_cpu}
        if selected:
            per_cpu = selected
    values = sorted(set(per_cpu.values()))
    summary = values[0] if len(values) == 1 else "mixed:" + ",".join(values)
    return summary, per_cpu, "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"


def _read_windows_power_plan():
    exe = shutil.which("powercfg")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "/getactivescheme"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"\(([^)]*)\)\s*$", proc.stdout.strip())
    return m.group(1).strip() if m else proc.stdout.strip() or None


def _read_throttle_counters(cpus=None):
    counters: dict = {}
    sources: list = []
    unreadable: list = []

    # x86 per-core and per-package thermal throttle counters.
    for kind in ("core_throttle_count", "package_throttle_count"):
        found = False
        for path in sorted(
            glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/" + kind)
        ):
            m = re.search(r"cpu(\d+)/thermal_throttle/", path.replace("\\", "/"))
            text = _read_text(path)
            if not (m and text):
                continue
            cpu = int(m.group(1))
            if cpus and cpu not in set(int(c) for c in cpus):
                continue
            try:
                counters["thermal:%s:cpu%d" % (kind, cpu)] = int(text.strip())
                found = True
            except ValueError:
                continue
        if found:
            sources.append("/sys/devices/system/cpu/cpu*/thermal_throttle/" + kind)

    # cgroup v2 CPU quota throttling, then v1.
    cgroup_v2 = _read_text("/sys/fs/cgroup/cpu.stat")
    if cgroup_v2:
        stats = parse_cgroup_cpu_stat(cgroup_v2)
        for key in ("nr_throttled", "throttled_usec"):
            if key in stats:
                counters["quota:%s" % key] = stats[key]
        if any(k.startswith("quota:") for k in counters):
            sources.append("/sys/fs/cgroup/cpu.stat (cgroup v2)")
    else:
        cgroup_v1 = _read_text("/sys/fs/cgroup/cpu/cpu.stat")
        if cgroup_v1:
            stats = parse_cgroup_cpu_stat(cgroup_v1)
            for key in ("nr_throttled", "throttled_time"):
                if key in stats:
                    counters["quota:%s" % key] = stats[key]
            if any(k.startswith("quota:") for k in counters):
                sources.append("/sys/fs/cgroup/cpu/cpu.stat (cgroup v1)")
        else:
            unreadable.append("cgroup cpu.stat (no v2 or v1 path readable)")

    # Raspberry Pi firmware throttle bitmask.
    vcgencmd = shutil.which("vcgencmd")
    if vcgencmd:
        try:
            proc = subprocess.run(
                [vcgencmd, "get_throttled"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            decoded = parse_vcgencmd_throttled(proc.stdout or "")
            if decoded:
                counters["thermal:vcgencmd_throttled_raw"] = decoded["raw"]
                sources.append("vcgencmd get_throttled")
        except (OSError, subprocess.SubprocessError):
            unreadable.append("vcgencmd get_throttled (could not run)")

    if not counters:
        unreadable.append(
            "thermal and quota throttle counters (none of "
            "cpu*/thermal_throttle/*, cgroup cpu.stat or vcgencmd readable)"
        )
    return counters, sources, unreadable


def _read_temperature():
    best = None
    for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        text = _read_text(path)
        if not text:
            continue
        try:
            milli = int(text.strip())
        except ValueError:
            continue
        celsius = milli / 1000.0 if abs(milli) > 200 else float(milli)
        best = celsius if best is None else max(best, celsius)
    return best


def sample_conditions(cpus=None) -> ConditionSnapshot:
    """Take one reading of everything that could invalidate a run."""
    unreadable: list = []
    governor, per_cpu, governor_source = _read_governor(cpus)
    power_plan = None
    if governor is None:
        if platform.system() == "Windows":
            power_plan = _read_windows_power_plan()
            unreadable.append(
                "CPU frequency governor (Linux cpufreq is not present on "
                "Windows; the active power plan is recorded instead)"
            )
            if power_plan is None:
                unreadable.append("powercfg /getactivescheme")
        else:
            unreadable.append(
                "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
            )

    counters, sources, counter_unreadable = _read_throttle_counters(cpus)
    unreadable.extend(counter_unreadable)

    swap = {}
    vmstat = _read_text("/proc/vmstat")
    if vmstat:
        swap = parse_vmstat(vmstat)
    if not swap:
        unreadable.append("/proc/vmstat swap counters")

    major_faults = parse_proc_self_stat_majflt(_read_text("/proc/self/stat") or "")
    if major_faults is None:
        unreadable.append("/proc/self/stat major fault count")

    temperature = _read_temperature()
    if temperature is None:
        unreadable.append("/sys/class/thermal/thermal_zone*/temp")

    return ConditionSnapshot(
        taken_at=time.time(),
        governor=governor,
        governor_per_cpu=per_cpu,
        governor_source=governor_source,
        throttle_counters=counters,
        throttle_sources=sources,
        swap=swap,
        major_faults=major_faults,
        temperature_c=temperature,
        power_plan=power_plan,
        unreadable=unreadable,
    )


# ---------------------------------------------------------------------------
# Competing load
# ---------------------------------------------------------------------------


def _windows_system_busy_fraction(window_s: float):
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    def value(ft):
        return (ft.high << 32) | ft.low

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        return None, None
    idle0, kernel0, user0 = FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetSystemTimes(
        ctypes.byref(idle0), ctypes.byref(kernel0), ctypes.byref(user0)
    ):
        return None, None
    time.sleep(window_s)
    idle1, kernel1, user1 = FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetSystemTimes(
        ctypes.byref(idle1), ctypes.byref(kernel1), ctypes.byref(user1)
    ):
        return None, None
    # The kernel time returned by GetSystemTimes includes idle time.
    total = (value(kernel1) - value(kernel0)) + (value(user1) - value(user0))
    idle = value(idle1) - value(idle0)
    if total <= 0:
        return None, None
    fraction = max(0.0, min(1.0, 1.0 - idle / float(total)))
    return fraction, "GetSystemTimes (whole system, not per target CPU)"


def measure_competing_load(cpus=None, window_s: float = 0.4):
    """Observe how busy the target CPUs are BEFORE memceiling loads them.

    Returns (busy_fraction, source, unreadable_list). The fraction is the
    non-idle fraction of CPU time over the window. On Linux it is restricted
    to the target CPUs. On Windows only a whole-system figure is available,
    and the source string says so, because a whole-system figure on a
    20-thread laptop understates a single busy core by a factor of 20.
    """
    first = _read_text("/proc/stat")
    if first is not None:
        before = parse_proc_stat(first)
        time.sleep(window_s)
        second = _read_text("/proc/stat")
        after = parse_proc_stat(second or "")
        fraction = busy_fraction_from_proc_stat(before, after, cpus)
        if fraction is None:
            return None, None, ["/proc/stat (snapshots did not cover target CPUs)"]
        scope = "target CPUs" if cpus else "all CPUs"
        return fraction, "/proc/stat per-CPU deltas over %.2f s (%s)" % (
            window_s,
            scope,
        ), []

    if platform.system() == "Windows":
        fraction, source = _windows_system_busy_fraction(window_s)
        if fraction is None:
            return None, None, ["GetSystemTimes"]
        return fraction, source, []

    return None, None, ["/proc/stat"]


# ---------------------------------------------------------------------------
# Governor control, opt-in only
# ---------------------------------------------------------------------------


class GovernorChange:
    """A governor change that knows how to undo itself and verify the undo.

    memceiling only constructs this when the operator passes an explicit opt-in
    flag. The restore is verified by reading the value back, and a failure to
    restore is reported as an invalidating condition rather than swallowed,
    because leaving a machine in a changed power state is a side effect the
    operator must hear about.
    """

    def __init__(self, target: str, cpus=None):
        self.target = target
        self.cpus = list(cpus) if cpus else None
        self.previous: dict = {}
        self.applied = False
        self.restored = False
        self.restore_verified = False
        self.errors: list = []

    def _paths(self):
        paths = []
        for path in sorted(
            glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
        ):
            m = re.search(r"cpu(\d+)/cpufreq/scaling_governor$", path.replace("\\", "/"))
            if not m:
                continue
            cpu = int(m.group(1))
            if self.cpus and cpu not in set(int(c) for c in self.cpus):
                continue
            paths.append((cpu, path))
        return paths

    def apply(self) -> bool:
        paths = self._paths()
        if not paths:
            self.errors.append(
                "no cpufreq scaling_governor file was found, so the governor "
                "was not changed"
            )
            return False
        for cpu, path in paths:
            current = _read_text(path)
            if current is None:
                self.errors.append("could not read %s" % path)
                continue
            self.previous[cpu] = current.strip()
            try:
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(self.target)
            except OSError as exc:
                self.errors.append("could not write %s: %s" % (path, exc))
        self.applied = bool(self.previous) and not self.errors
        return self.applied

    def restore(self) -> bool:
        if not self.previous:
            self.restored = True
            self.restore_verified = True
            return True
        ok = True
        for cpu, value in self.previous.items():
            path = "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor" % cpu
            try:
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(value)
            except OSError as exc:
                self.errors.append("could not restore %s: %s" % (path, exc))
                ok = False
                continue
            readback = _read_text(path)
            if readback is None or readback.strip() != value:
                self.errors.append(
                    "restore of %s did not verify: wanted %r, read back %r"
                    % (path, value, (readback or "").strip())
                )
                ok = False
        self.restored = ok
        self.restore_verified = ok
        return ok

    def as_dict(self) -> dict:
        return {
            "requested_governor": self.target,
            "applied": self.applied,
            "previous_per_cpu": dict(self.previous),
            "restored": self.restored,
            "restore_verified": self.restore_verified,
            "errors": list(self.errors),
        }
