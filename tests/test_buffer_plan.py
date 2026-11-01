"""Tests for buffer sizing, slicing and the pass-count calibration.

The buffer plan is where the working-set-versus-cache refusal is decided, so
these tests drive it with explicit byte counts rather than a real machine.
"""

import pytest

from memceiling.kernels import (
    CACHE_LINE_BYTES,
    DEFAULT_LLC_MARGIN,
    ELEMENT_BYTES,
    INNER_SAFETY_FACTOR,
    MAX_INNER_PASSES,
    STREAM_MIN_ELEMENTS,
    assign_cpus,
    choose_inner_passes,
    plan_buffers,
    split_evenly,
)

MIB = 1024 * 1024


class TestMarginRule:
    def test_sizes_each_array_to_four_times_the_summed_llc(self):
        plan = plan_buffers(llc_total_bytes=24 * MIB, available_bytes=32 * 1024 * MIB)
        assert plan.margin == DEFAULT_LLC_MARGIN
        assert plan.required_bytes_per_array == 96 * MIB
        assert plan.bytes_per_array >= 96 * MIB
        assert plan.satisfies_margin is True
        assert plan.llc_known is True

    def test_honours_a_larger_margin(self):
        plan = plan_buffers(
            llc_total_bytes=8 * MIB, margin=10.0, available_bytes=32 * 1024 * MIB
        )
        assert plan.required_bytes_per_array == 80 * MIB
        assert plan.bytes_per_array >= 80 * MIB
        assert plan.satisfies_margin is True

    def test_applies_streams_one_million_element_floor_on_a_tiny_cache(self):
        plan = plan_buffers(llc_total_bytes=256 * 1024, available_bytes=32 * 1024 * MIB)
        assert plan.elements_per_array >= STREAM_MIN_ELEMENTS
        assert plan.satisfies_margin is True

    def test_array_size_is_a_whole_number_of_cache_lines(self):
        plan = plan_buffers(llc_total_bytes=24 * MIB, available_bytes=32 * 1024 * MIB)
        assert plan.bytes_per_array % CACHE_LINE_BYTES == 0
        assert plan.bytes_per_array == plan.elements_per_array * ELEMENT_BYTES


class TestRefusalCases:
    def test_unknown_llc_uses_a_floor_and_does_not_claim_the_margin(self):
        plan = plan_buffers(llc_total_bytes=None, available_bytes=32 * 1024 * MIB)
        assert plan.llc_known is False
        assert plan.satisfies_margin is False
        assert plan.required_bytes_per_array is None
        assert "NOT verified" in plan.rationale

    def test_a_memory_cap_that_breaks_the_margin_is_reported(self):
        # A 64 MiB LLC needs 256 MiB per array, but only 300 MiB is available
        # and memceiling will use at most half of it across three arrays.
        plan = plan_buffers(llc_total_bytes=64 * MIB, available_bytes=300 * MIB)
        assert plan.memory_limited is True
        assert plan.satisfies_margin is False
        assert plan.bytes_per_array < plan.required_bytes_per_array
        assert "capped at" in plan.rationale

    def test_severe_memory_shortage_sets_insufficient_memory(self):
        plan = plan_buffers(llc_total_bytes=24 * MIB, available_bytes=8 * MIB)
        assert plan.insufficient_memory is True
        assert plan.elements_per_array < STREAM_MIN_ELEMENTS

    def test_plenty_of_memory_does_not_set_either_flag(self):
        plan = plan_buffers(llc_total_bytes=24 * MIB, available_bytes=32 * 1024 * MIB)
        assert plan.memory_limited is False
        assert plan.insufficient_memory is False


class TestExplicitSize:
    def test_an_explicit_size_overrides_the_margin_target(self):
        plan = plan_buffers(
            llc_total_bytes=24 * MIB,
            available_bytes=32 * 1024 * MIB,
            requested_bytes_per_array=200 * MIB,
        )
        assert plan.bytes_per_array == 200 * MIB
        assert "set explicitly" in plan.rationale

    def test_an_explicit_size_is_still_checked_against_the_margin(self):
        plan = plan_buffers(
            llc_total_bytes=24 * MIB,
            available_bytes=32 * 1024 * MIB,
            requested_bytes_per_array=8 * MIB,
        )
        # Below 4x the 24 MiB cache, so the margin is not satisfied and the
        # caller must refuse. An explicit flag does not buy a clean verdict.
        assert plan.satisfies_margin is False

    def test_total_bytes_counts_every_array(self):
        plan = plan_buffers(
            llc_total_bytes=24 * MIB,
            arrays=3,
            available_bytes=32 * 1024 * MIB,
            requested_bytes_per_array=100 * MIB,
        )
        assert plan.total_bytes == 300 * MIB


class TestSlicing:
    def test_slices_cover_the_array_exactly(self):
        for total in (1_000_000, 12_582_912, 999_983):
            for parts in (1, 2, 3, 4, 7, 20):
                slices = split_evenly(total, parts)
                assert len(slices) == parts
                assert slices[0][0] == 0
                assert sum(length for _, length in slices) == total
                for index in range(1, parts):
                    assert slices[index][0] == (
                        slices[index - 1][0] + slices[index - 1][1]
                    )

    def test_interior_boundaries_land_on_cache_lines(self):
        per_line = CACHE_LINE_BYTES // ELEMENT_BYTES
        slices = split_evenly(12_582_912, 20)
        for start, _ in slices[1:]:
            assert start % per_line == 0

    def test_single_part_is_the_whole_array(self):
        assert split_evenly(1234, 1) == [(0, 1234)]

    def test_zero_parts(self):
        assert split_evenly(1000, 0) == []


class TestCpuAssignment:
    def test_one_thread_per_cpu_by_default(self):
        assert assign_cpus([0, 1, 2, 3], None) == [[0], [1], [2], [3]]

    def test_each_thread_gets_exactly_one_cpu(self):
        for assignment in assign_cpus([4, 5, 6], None):
            assert len(assignment) == 1

    def test_fewer_threads_than_cpus(self):
        assert assign_cpus([0, 1, 2, 3], 2) == [[0], [1]]

    def test_more_threads_than_cpus_reuses_round_robin(self):
        assert assign_cpus([0, 1], 5) == [[0], [1], [0], [1], [0]]

    def test_cpus_are_deduplicated_and_sorted(self):
        assert assign_cpus([3, 1, 1, 2], None) == [[1], [2], [3]]

    def test_empty_cpu_set(self):
        assert assign_cpus([], None) == []


class TestInnerPasses:
    def test_a_short_calibration_raises_the_pass_count(self):
        inner = choose_inner_passes(calibration_s=0.005, min_run_seconds=0.2)
        assert inner == pytest.approx(50, abs=1)
        assert inner * 0.005 >= 0.2

    def test_a_long_calibration_needs_one_pass(self):
        assert choose_inner_passes(calibration_s=1.0, min_run_seconds=0.2) == 1

    def test_the_result_clears_the_floor_with_the_safety_factor(self):
        for calibration in (0.001, 0.004, 0.02, 0.1):
            inner = choose_inner_passes(calibration, 0.2)
            if inner < MAX_INNER_PASSES:
                assert inner * calibration >= 0.2 * INNER_SAFETY_FACTOR * 0.999

    def test_is_capped(self):
        assert choose_inner_passes(1e-9, 10.0) == MAX_INNER_PASSES

    @pytest.mark.parametrize("calibration", [None, 0.0, -1.0])
    def test_an_unusable_calibration_falls_back_to_one_pass(self, calibration):
        assert choose_inner_passes(calibration, 0.2) == 1
