"""Tests for the theoretical peak derivation.

These check the arithmetic and the refusal to guess. The two worked examples
are the ones the module's docstring states, so the documented formula and
the code cannot drift apart.
"""

import pytest

from memceiling.theoretical import (
    FORMULA,
    MemoryModule,
    parse_dmidecode_memory,
    parse_memory_config_string,
    peak_from_modules,
)

DMIDECODE_TWO_DDR5 = """# dmidecode 3.5
Getting SMBIOS data from sysfs.
SMBIOS 3.4.0 present.

Handle 0x1000, DMI type 16, 23 bytes
Physical Memory Array
\tLocation: System Board Or Motherboard
\tUse: System Memory
\tMaximum Capacity: 64 GB

Handle 0x1100, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x1000
\tTotal Width: 64 bits
\tData Width: 64 bits
\tSize: 16 GB
\tForm Factor: SODIMM
\tLocator: DIMM A
\tType: DDR5
\tSpeed: 4800 MT/s
\tManufacturer: Example
\tConfigured Memory Speed: 4800 MT/s

Handle 0x1101, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x1000
\tTotal Width: 64 bits
\tData Width: 64 bits
\tSize: 16 GB
\tLocator: DIMM B
\tType: DDR5
\tSpeed: 4800 MT/s
\tConfigured Memory Speed: 4800 MT/s
"""

DMIDECODE_ONE_POPULATED_ONE_EMPTY = """Handle 0x1100, DMI type 17, 92 bytes
Memory Device
\tTotal Width: 72 bits
\tData Width: 64 bits
\tSize: 8 GB
\tLocator: DIMM 0
\tType: DDR4
\tSpeed: 3200 MT/s
\tConfigured Memory Speed: 2933 MT/s

Handle 0x1101, DMI type 17, 92 bytes
Memory Device
\tTotal Width: Unknown
\tData Width: Unknown
\tSize: No Module Installed
\tLocator: DIMM 1
\tType: Unknown
"""


class TestDmidecodeParsing:
    def test_reads_both_populated_modules(self):
        modules = parse_dmidecode_memory(DMIDECODE_TWO_DDR5)
        assert len(modules) == 2
        assert [m.locator for m in modules] == ["DIMM A", "DIMM B"]
        assert all(m.data_width_bits == 64 for m in modules)
        assert all(m.configured_rate_mt_s == 4800 for m in modules)
        assert all(m.mem_type == "DDR5" for m in modules)

    def test_skips_unpopulated_slots(self):
        modules = parse_dmidecode_memory(DMIDECODE_ONE_POPULATED_ONE_EMPTY)
        assert len(modules) == 1
        assert modules[0].locator == "DIMM 0"
        assert modules[0].total_width_bits == 72
        assert modules[0].data_width_bits == 64

    def test_empty_text(self):
        assert parse_dmidecode_memory("") == []


class TestPeakArithmetic:
    def test_two_ddr5_4800_modules_give_76_8(self):
        """The worked example from the module docstring."""
        peak = peak_from_modules(parse_dmidecode_memory(DMIDECODE_TWO_DDR5))
        assert peak.known
        assert peak.peak_gb_s == pytest.approx(76.8)
        assert peak.inputs["total_data_width_bits"] == 128
        assert peak.inputs["data_rate_mt_s"] == 4800
        assert peak.formula == FORMULA

    def test_one_lpddr4x_4267_32_bit_channel_gives_17_068(self):
        """The second worked example from the module docstring."""
        peak = peak_from_modules(parse_memory_config_string("4267:32:1"))
        assert peak.peak_gb_s == pytest.approx(17.068)

    def test_prefers_the_configured_rate_over_the_rated_one(self):
        peak = peak_from_modules(
            parse_dmidecode_memory(DMIDECODE_ONE_POPULATED_ONE_EMPTY)
        )
        # 2933 MT/s configured, not the 3200 MT/s the module is rated for.
        assert peak.inputs["data_rate_mt_s"] == 2933
        assert peak.peak_gb_s == pytest.approx(2933 * 64 / 8 / 1000)

    def test_excludes_ecc_check_bits_by_using_data_width(self):
        peak = peak_from_modules(
            [MemoryModule(data_width_bits=64, total_width_bits=72, configured_rate_mt_s=3200)]
        )
        assert peak.inputs["total_data_width_bits"] == 64

    def test_mixed_rates_use_the_slowest_and_say_so(self):
        peak = peak_from_modules(
            [
                MemoryModule(data_width_bits=64, configured_rate_mt_s=4800),
                MemoryModule(data_width_bits=64, configured_rate_mt_s=3200),
            ]
        )
        assert peak.inputs["data_rate_mt_s"] == 3200
        assert "different transfer rates" in peak.note


class TestRefusalToGuess:
    def test_no_modules_is_unknown(self):
        peak = peak_from_modules([])
        assert not peak.known
        assert peak.peak_gb_s is None
        assert peak.unreadable

    def test_missing_rate_is_unknown_not_a_default(self):
        peak = peak_from_modules([MemoryModule(data_width_bits=64)])
        assert not peak.known
        assert "memory transfer rate (MT/s)" in peak.unreadable

    def test_missing_width_is_unknown_not_a_default(self):
        peak = peak_from_modules([MemoryModule(configured_rate_mt_s=4800)])
        assert not peak.known
        assert "memory module data width" in peak.unreadable

    def test_a_partially_readable_population_is_flagged_as_a_lower_bound(self):
        peak = peak_from_modules(
            [
                MemoryModule(data_width_bits=64, configured_rate_mt_s=4800),
                MemoryModule(configured_rate_mt_s=4800),
            ]
        )
        assert peak.known
        assert peak.inputs["module_count"] == 1
        assert "lower bound" in peak.note


class TestMemoryConfigOverride:
    def test_three_field_form(self):
        modules = parse_memory_config_string("4800:64:2")
        assert len(modules) == 2
        assert all(m.data_width_bits == 64 for m in modules)
        assert all(m.configured_rate_mt_s == 4800 for m in modules)

    def test_four_field_form_records_the_type(self):
        modules = parse_memory_config_string("4267:32:1:LPDDR4X")
        assert modules[0].mem_type == "LPDDR4X"

    @pytest.mark.parametrize(
        "text",
        ["", "4800", "4800:64", "4800:64:2:LPDDR4X:extra", "a:b:c", "0:64:2", "4800:0:1", "4800:64:0"],
    )
    def test_rejects_malformed_input_loudly(self, text):
        with pytest.raises(ValueError):
            parse_memory_config_string(text)

    def test_an_override_is_labelled_as_an_override(self):
        from memceiling.theoretical import derive_theoretical_peak

        peak = derive_theoretical_peak("4267:32:1")
        assert peak.peak_gb_s == pytest.approx(17.068)
        assert "override" in (peak.source or "")
        assert "not from a reading of this machine" in peak.note
