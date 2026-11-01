"""Parsing tests.

Every parser memceiling uses to read a machine is exercised here against fixed
text, so the test suite proves the reading logic without depending on the
machine the tests run on. None of these tests asserts a throughput value.
"""

import pytest

from memceiling.conditions import (
    busy_fraction_from_proc_stat,
    parse_cgroup_cpu_stat,
    parse_proc_self_stat_majflt,
    parse_proc_stat,
    parse_vcgencmd_throttled,
    parse_vmstat,
)
from memceiling.topology import (
    format_cpu_list,
    group_by_capacity,
    parse_cache_size,
    parse_cpu_list,
    parse_cpuinfo_arm_clusters,
    parse_meminfo,
    select_llc,
)

# Synthetic /proc/cpuinfo fixture for an Arm big.LITTLE part: four
# Cortex-A73 (part 0xd09) and four Cortex-A53 (part 0xd03). It is written by
# hand to exercise the grouping code and is not a capture of any machine.
BIG_LITTLE_CPUINFO = """processor\t: 0
BogoMIPS\t: 26.00
Features\t: fp asimd evtstrm aes
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4

processor\t: 1
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4

processor\t: 2
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4

processor\t: 3
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4

processor\t: 4
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd09
CPU revision\t: 2

processor\t: 5
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd09
CPU revision\t: 2

processor\t: 6
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd09
CPU revision\t: 2

processor\t: 7
CPU implementer\t: 0x41
CPU variant\t: 0x0
CPU part\t: 0xd09
CPU revision\t: 2
"""

# Synthetic /proc/cpuinfo for a homogeneous quad Cortex-A76 (part 0xd0b).
HOMOGENEOUS_CPUINFO = "\n\n".join(
    "processor\t: %d\nCPU implementer\t: 0x41\nCPU part\t: 0xd0b" % cpu
    for cpu in range(4)
)

X86_CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Some x86 CPU
flags\t\t: fpu vme de

processor\t: 1
vendor_id\t: GenuineIntel
model name\t: Some x86 CPU
"""


class TestCpuLists:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0-3", (0, 1, 2, 3)),
            ("0-3,8", (0, 1, 2, 3, 8)),
            ("5", (5,)),
            ("0-1,4-5,9", (0, 1, 4, 5, 9)),
            ("", ()),
            ("   ", ()),
            ("3-1", (1, 2, 3)),
            ("2,2,2", (2,)),
        ],
    )
    def test_parse_cpu_list(self, text, expected):
        assert parse_cpu_list(text) == expected

    @pytest.mark.parametrize(
        "cpus,expected",
        [
            ([0, 1, 2, 3], "0-3"),
            ([0, 1, 2, 3, 8], "0-3,8"),
            ([5], "5"),
            ([], ""),
            ([9, 4, 5, 1, 0], "0-1,4-5,9"),
        ],
    )
    def test_format_cpu_list(self, cpus, expected):
        assert format_cpu_list(cpus) == expected

    def test_roundtrip(self):
        for text in ("0-11", "0-3,12-19", "1,3,5,7"):
            assert format_cpu_list(parse_cpu_list(text)) == text


class TestCacheSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("32K", 32768),
            ("8192K", 8388608),
            ("4M", 4 * 1024 * 1024),
            ("24 MiB", 24 * 1024 * 1024),
            ("262144", 262144),
            ("1.5M", int(1.5 * 1024 * 1024)),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_cache_size(text) == expected

    @pytest.mark.parametrize("text", ["", "unknown", None, "lots", "12 furlongs"])
    def test_rejects(self, text):
        assert parse_cache_size(text) is None


class TestArmClusters:
    def test_splits_big_little_into_two_clusters(self):
        groups = parse_cpuinfo_arm_clusters(BIG_LITTLE_CPUINFO)
        assert len(groups) == 2
        assert [g["cpus"] for g in groups] == [(0, 1, 2, 3), (4, 5, 6, 7)]
        assert [g["name"] for g in groups] == ["Cortex-A53", "Cortex-A73"]
        assert [g["part"] for g in groups] == [0xD03, 0xD09]
        assert all(g["implementer"] == 0x41 for g in groups)

    def test_homogeneous_is_one_cluster(self):
        groups = parse_cpuinfo_arm_clusters(HOMOGENEOUS_CPUINFO)
        assert len(groups) == 1
        assert groups[0]["name"] == "Cortex-A76"
        assert groups[0]["cpus"] == (0, 1, 2, 3)

    def test_x86_cpuinfo_yields_nothing(self):
        assert parse_cpuinfo_arm_clusters(X86_CPUINFO) == []

    def test_empty_text_yields_nothing(self):
        assert parse_cpuinfo_arm_clusters("") == []

    def test_unknown_part_still_clusters(self):
        text = (
            "processor\t: 0\nCPU implementer\t: 0x41\nCPU part\t: 0xfff\n\n"
            "processor\t: 1\nCPU implementer\t: 0x41\nCPU part\t: 0xd03\n"
        )
        groups = parse_cpuinfo_arm_clusters(text)
        assert len(groups) == 2
        names = {g["name"] for g in groups}
        assert "part-0xfff" in names
        assert "Cortex-A53" in names

    def test_variant_does_not_split_a_cluster(self):
        text = (
            "processor\t: 0\nCPU implementer\t: 0x41\nCPU variant\t: 0x0\n"
            "CPU part\t: 0xd0b\n\n"
            "processor\t: 1\nCPU implementer\t: 0x41\nCPU variant\t: 0x1\n"
            "CPU part\t: 0xd0b\n"
        )
        groups = parse_cpuinfo_arm_clusters(text)
        assert len(groups) == 1
        assert groups[0]["cpus"] == (0, 1)


class TestCapacityGrouping:
    def test_two_capacities_two_clusters_small_first(self):
        groups = group_by_capacity({0: 1024, 1: 1024, 2: 383, 3: 383})
        assert [g["capacity"] for g in groups] == [383, 1024]
        assert [g["cpus"] for g in groups] == [(2, 3), (0, 1)]

    def test_single_capacity_single_cluster(self):
        groups = group_by_capacity({0: 1024, 1: 1024})
        assert len(groups) == 1

    def test_empty(self):
        assert group_by_capacity({}) == []


class TestSelectLlc:
    def test_picks_highest_unified_level_and_sums_distinct_instances(self):
        entries = [
            {"level": 1, "size_bytes": 32768, "type": "Data", "shared_cpus": (0,)},
            {"level": 1, "size_bytes": 32768, "type": "Instruction", "shared_cpus": (0,)},
            {"level": 2, "size_bytes": 1048576, "type": "Unified", "shared_cpus": (0, 1)},
            {"level": 3, "size_bytes": 8388608, "type": "Unified", "shared_cpus": (0, 1)},
            {"level": 3, "size_bytes": 8388608, "type": "Unified", "shared_cpus": (2, 3)},
        ]
        llc = select_llc(entries)
        assert llc["level"] == 3
        assert llc["instances"] == 2
        assert llc["total_bytes"] == 2 * 8388608

    def test_counts_a_shared_cache_once_even_when_reported_per_cpu(self):
        entries = [
            {"level": 3, "size_bytes": 25165824, "type": "Unified", "shared_cpus": (0, 1, 2, 3)}
            for _ in range(4)
        ]
        llc = select_llc(entries)
        assert llc["instances"] == 1
        assert llc["total_bytes"] == 25165824

    def test_restricts_the_sum_to_the_selected_cpus(self):
        entries = [
            {"level": 3, "size_bytes": 8388608, "type": "Unified", "shared_cpus": (0, 1)},
            {"level": 3, "size_bytes": 8388608, "type": "Unified", "shared_cpus": (2, 3)},
        ]
        assert select_llc(entries, selected_cpus=[0, 1])["total_bytes"] == 8388608
        assert select_llc(entries, selected_cpus=[0, 2])["total_bytes"] == 2 * 8388608

    def test_ignores_instruction_only_levels(self):
        entries = [
            {"level": 2, "size_bytes": 1048576, "type": "Unified", "shared_cpus": (0,)},
            {"level": 3, "size_bytes": 999, "type": "Instruction", "shared_cpus": (0,)},
        ]
        assert select_llc(entries)["level"] == 2

    def test_no_entries_yields_empty(self):
        assert select_llc([]) == {}
        assert select_llc([{"level": 1, "size_bytes": 0, "type": "Unified"}]) == {}


class TestMeminfo:
    TEXT = """MemTotal:       32827504 kB
MemFree:         1204352 kB
MemAvailable:    8192000 kB
Buffers:          100000 kB
SwapTotal:       2097152 kB
Hugepagesize:       2048 kB
"""

    def test_converts_kib_to_bytes(self):
        parsed = parse_meminfo(self.TEXT)
        assert parsed["total_bytes"] == 32827504 * 1024
        assert parsed["available_bytes"] == 8192000 * 1024
        assert parsed["swap_total_bytes"] == 2097152 * 1024
        assert parsed["hugepage_bytes"] == 2048 * 1024

    def test_empty_text(self):
        assert parse_meminfo("") == {}


class TestProcStat:
    BEFORE = """cpu  100 0 100 1000 0 0 0 0 0 0
cpu0 10 0 10 100 0 0 0 0 0 0
cpu1 10 0 10 100 0 0 0 0 0 0
intr 1234
"""
    AFTER_IDLE = """cpu  100 0 100 1200 0 0 0 0 0 0
cpu0 10 0 10 200 0 0 0 0 0 0
cpu1 10 0 10 200 0 0 0 0 0 0
intr 1300
"""
    AFTER_BUSY = """cpu  200 0 100 1000 0 0 0 0 0 0
cpu0 110 0 10 100 0 0 0 0 0 0
cpu1 10 0 10 200 0 0 0 0 0 0
"""

    def test_parses_per_cpu_and_aggregate(self):
        parsed = parse_proc_stat(self.BEFORE)
        assert -1 in parsed and 0 in parsed and 1 in parsed
        assert parsed[0][0] == 10

    def test_fully_idle_window_is_zero_busy(self):
        before = parse_proc_stat(self.BEFORE)
        after = parse_proc_stat(self.AFTER_IDLE)
        assert busy_fraction_from_proc_stat(before, after) == 0.0

    def test_one_busy_cpu_shows_as_busy_on_that_cpu_only(self):
        before = parse_proc_stat(self.BEFORE)
        after = parse_proc_stat(self.AFTER_BUSY)
        assert busy_fraction_from_proc_stat(before, after, cpus=[0]) == 1.0
        assert busy_fraction_from_proc_stat(before, after, cpus=[1]) == 0.0

    def test_iowait_counts_as_idle(self):
        before = parse_proc_stat("cpu0 0 0 0 0 0 0 0 0")
        after = parse_proc_stat("cpu0 0 0 0 0 100 0 0 0")
        assert busy_fraction_from_proc_stat(before, after, cpus=[0]) == 0.0

    def test_counter_going_backwards_is_rejected(self):
        before = parse_proc_stat("cpu0 100 0 0 100 0 0 0 0")
        after = parse_proc_stat("cpu0 50 0 0 100 0 0 0 0")
        assert busy_fraction_from_proc_stat(before, after, cpus=[0]) is None

    def test_no_elapsed_time_is_rejected(self):
        snapshot = parse_proc_stat(self.BEFORE)
        assert busy_fraction_from_proc_stat(snapshot, snapshot, cpus=[0]) is None


class TestCgroupAndVmstat:
    def test_cgroup_v2_cpu_stat(self):
        text = (
            "usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\n"
            "nr_periods 10\nnr_throttled 3\nthrottled_usec 4567\n"
        )
        parsed = parse_cgroup_cpu_stat(text)
        assert parsed["nr_throttled"] == 3
        assert parsed["throttled_usec"] == 4567

    def test_cgroup_garbage_lines_are_skipped(self):
        assert parse_cgroup_cpu_stat("not a pair\nnr_throttled x\nok 1\n") == {"ok": 1}

    def test_vmstat_keeps_only_swap_counters(self):
        text = "nr_free_pages 1000\npswpin 12\npswpout 34\npgfault 9999\n"
        assert parse_vmstat(text) == {"pswpin": 12, "pswpout": 34}

    def test_vmstat_empty(self):
        assert parse_vmstat("") == {}


class TestProcSelfStat:
    def test_extracts_major_fault_field(self):
        # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt
        # majflt is field 12, so the eleventh value after the command name.
        text = "4242 (python) S 1 4242 4242 0 -1 4194304 500 0 77 0 10 2 0 0"
        assert parse_proc_self_stat_majflt(text) == 77

    def test_handles_a_command_name_containing_spaces_and_parens(self):
        text = "7 (my (odd) name) S 1 7 7 0 -1 0 1 0 9 0 0 0 0 0"
        assert parse_proc_self_stat_majflt(text) == 9

    @pytest.mark.parametrize("text", ["", "no parens here", "1 (x) S"])
    def test_rejects_unparseable(self, text):
        assert parse_proc_self_stat_majflt(text) is None


class TestVcgencmd:
    def test_decodes_sticky_and_live_bits(self):
        decoded = parse_vcgencmd_throttled("throttled=0x50005")
        assert decoded["raw"] == 0x50005
        assert decoded["under_voltage_now"] is True
        assert decoded["currently_throttled"] is True
        assert decoded["under_voltage_occurred"] is True
        assert decoded["throttling_occurred"] is True
        assert decoded["arm_frequency_capped_now"] is False

    def test_clean_machine(self):
        decoded = parse_vcgencmd_throttled("throttled=0x0")
        assert decoded["raw"] == 0
        assert not any(v for k, v in decoded.items() if k != "raw")

    def test_rejects_other_text(self):
        assert parse_vcgencmd_throttled("temp=48.0'C") is None
        assert parse_vcgencmd_throttled("") is None
