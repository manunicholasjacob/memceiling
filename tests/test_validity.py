"""Tests for the refusal rules.

The verdict logic is pure, so every refusal memceiling claims to make is asserted
here directly: a working set inside the cache, a throttle counter that
advanced, a governor that changed, an affinity mask that did not take, a
figure above theoretical. No test here depends on how fast any machine is.
"""

import pytest

from memceiling.schema import (
    CONFIDENCE_BLOCKING,
    REASON_CODES,
    SEVERITIES,
    SEVERITY_INVALIDATING,
    VERDICT_INVALID,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    VERDICT_UNSUPPORTED,
)
from memceiling.validity import ValidityInput, build_reasons, evaluate, verdict_from_reasons

MIB = 1024 * 1024


def clean_input(**overrides) -> ValidityInput:
    """A ValidityInput that should produce PASS, for one-factor-at-a-time tests."""
    base = dict(
        platform_supported=True,
        llc_known=True,
        llc_total_bytes=24 * MIB,
        bytes_per_array=96 * MIB,
        required_bytes_per_array=96 * MIB,
        satisfies_margin=True,
        insufficient_memory=False,
        pinning_supported=True,
        affinity_as_requested=True,
        busy_fraction=0.01,
        busy_source="/proc/stat",
        governor="performance",
        governor_changed=False,
        governor_before="performance",
        governor_after="performance",
        throttle_counters_readable=True,
        thermal_throttled=False,
        quota_throttled=False,
        swap_delta=0,
        major_fault_delta=0,
        peak_known=True,
        peak_gb_s=76.8,
        achieved_gb_s=53.9,
        achieved_kernel="copy",
        code_path="c",
        timer_resolution_s=1e-7,
        best_seconds=0.25,
        max_spread_fraction=0.02,
        clusters_detected=2,
        cluster_source="/proc/cpuinfo CPU implementer and CPU part",
    )
    base.update(overrides)
    return ValidityInput(**base)


def codes(reasons):
    return {r["code"] for r in reasons}


class TestCleanRunPasses:
    def test_a_clean_run_passes_with_no_reasons(self):
        reasons, verdict = evaluate(clean_input())
        assert reasons == []
        assert verdict == VERDICT_PASS


class TestWorkingSetRefusal:
    def test_working_set_below_llc_is_invalidating(self):
        reasons, verdict = evaluate(
            clean_input(satisfies_margin=False, bytes_per_array=8 * MIB)
        )
        assert "WORKING_SET_BELOW_LLC" in codes(reasons)
        assert verdict == VERDICT_INVALID

    def test_the_detail_names_both_sizes_and_the_margin(self):
        reasons = build_reasons(
            clean_input(satisfies_margin=False, bytes_per_array=8 * MIB)
        )
        detail = next(r["detail"] for r in reasons if r["code"] == "WORKING_SET_BELOW_LLC")
        assert str(8 * MIB) in detail
        assert str(96 * MIB) in detail
        assert "4.0x" in detail

    def test_unknown_llc_is_unknown_not_pass_and_not_invalid(self):
        reasons, verdict = evaluate(clean_input(llc_known=False, satisfies_margin=False))
        assert "LLC_SIZE_UNKNOWN" in codes(reasons)
        assert "WORKING_SET_BELOW_LLC" not in codes(reasons)
        assert verdict == VERDICT_UNKNOWN

    def test_insufficient_memory_is_invalidating(self):
        reasons, verdict = evaluate(clean_input(insufficient_memory=True))
        assert "MEMORY_INSUFFICIENT" in codes(reasons)
        assert verdict == VERDICT_INVALID


class TestCompetingLoad:
    def test_load_above_the_threshold_is_invalidating(self):
        reasons, verdict = evaluate(clean_input(busy_fraction=0.42))
        assert "COMPETING_LOAD" in codes(reasons)
        assert verdict == VERDICT_INVALID

    def test_load_at_the_threshold_is_accepted(self):
        reasons, verdict = evaluate(clean_input(busy_fraction=0.10, busy_threshold=0.10))
        assert "COMPETING_LOAD" not in codes(reasons)
        assert verdict == VERDICT_PASS

    def test_the_threshold_is_configurable(self):
        reasons, _ = evaluate(clean_input(busy_fraction=0.20, busy_threshold=0.50))
        assert "COMPETING_LOAD" not in codes(reasons)

    def test_unreadable_load_blocks_confidence(self):
        reasons, verdict = evaluate(clean_input(busy_fraction=None))
        assert "LOAD_UNREADABLE" in codes(reasons)
        assert verdict == VERDICT_UNKNOWN

    def test_the_detail_names_the_source(self):
        reasons = build_reasons(
            clean_input(busy_fraction=0.42, busy_source="GetSystemTimes (whole system)")
        )
        detail = next(r["detail"] for r in reasons if r["code"] == "COMPETING_LOAD")
        assert "GetSystemTimes" in detail


class TestThrottling:
    def test_thermal_throttle_during_the_run_is_invalidating(self):
        reasons, verdict = evaluate(
            clean_input(
                thermal_throttled=True,
                throttle_deltas={"thermal:core_throttle_count:cpu0": 3},
            )
        )
        assert "THERMAL_THROTTLE_DURING_RUN" in codes(reasons)
        assert verdict == VERDICT_INVALID
        detail = next(
            r["detail"] for r in reasons if r["code"] == "THERMAL_THROTTLE_DURING_RUN"
        )
        assert "cpu0" in detail

    def test_quota_throttle_during_the_run_is_invalidating(self):
        reasons, verdict = evaluate(
            clean_input(quota_throttled=True, throttle_deltas={"quota:nr_throttled": 2})
        )
        assert "QUOTA_THROTTLE_DURING_RUN" in codes(reasons)
        assert verdict == VERDICT_INVALID

    def test_unreadable_counters_block_confidence(self):
        reasons, verdict = evaluate(clean_input(throttle_counters_readable=False))
        assert "THROTTLE_COUNTERS_UNREADABLE" in codes(reasons)
        assert verdict == VERDICT_UNKNOWN

    def test_swap_activity_is_invalidating(self):
        reasons, verdict = evaluate(clean_input(swap_delta=17))
        assert "SWAP_ACTIVE_DURING_RUN" in codes(reasons)
        assert verdict == VERDICT_INVALID

    def test_major_faults_are_a_warning_only(self):
        reasons, verdict = evaluate(clean_input(major_fault_delta=4))
        assert "MAJOR_FAULTS_DURING_RUN" in codes(reasons)
        assert verdict == VERDICT_PASS


class TestGovernor:
    def test_a_governor_change_across_the_run_is_invalidating(self):
        reasons, verdict = evaluate(
            clean_input(
                governor_changed=True,
                governor_before="performance",
                governor_after="powersave",
            )
        )
        assert "GOVERNOR_CHANGED_DURING_RUN" in codes(reasons)
        assert verdict == VERDICT_INVALID

    def test_a_non_performance_governor_is_a_warning_and_still_passes(self):
        reasons, verdict = evaluate(clean_input(governor="schedutil"))
        assert "GOVERNOR_NOT_PERFORMANCE" in codes(reasons)
        assert verdict == VERDICT_PASS

    def test_a_mixed_governor_across_target_cpus_is_flagged(self):
        reasons, _ = evaluate(clean_input(governor="mixed:performance,powersave"))
        detail = next(
            r["detail"] for r in reasons if r["code"] == "GOVERNOR_NOT_PERFORMANCE"
        )
        assert "did not share one governor" in detail

    def test_an_unreadable_governor_blocks_confidence(self):
        reasons, verdict = evaluate(clean_input(governor=None))
        assert "GOVERNOR_UNKNOWN" in codes(reasons)
        assert verdict == VERDICT_UNKNOWN

    def test_a_failed_restore_is_invalidating_and_says_to_check_the_machine(self):
        reasons, verdict = evaluate(clean_input(governor_restore_verified=False))
        assert "GOVERNOR_RESTORE_FAILED" in codes(reasons)
        assert verdict == VERDICT_INVALID
        detail = next(
            r["detail"] for r in reasons if r["code"] == "GOVERNOR_RESTORE_FAILED"
        )
        assert "check it" in detail

    def test_a_verified_restore_adds_nothing(self):
        reasons, verdict = evaluate(clean_input(governor_restore_verified=True))
        assert "GOVERNOR_RESTORE_FAILED" not in codes(reasons)
        assert verdict == VERDICT_PASS


class TestAffinity:
    def test_a_mask_that_did_not_take_is_invalidating(self):
        reasons, verdict = evaluate(
            clean_input(
                affinity_as_requested=False,
                affinity_detail="requested 0-11, effective 0-3",
            )
        )
        assert "AFFINITY_NOT_AS_REQUESTED" in codes(reasons)
        assert verdict == VERDICT_INVALID
        detail = next(
            r["detail"] for r in reasons if r["code"] == "AFFINITY_NOT_AS_REQUESTED"
        )
        assert "effective 0-3" in detail

    def test_no_pinning_mechanism_blocks_confidence_rather_than_invalidating(self):
        reasons, verdict = evaluate(
            clean_input(pinning_supported=False, affinity_as_requested=False)
        )
        assert "AFFINITY_UNSUPPORTED" in codes(reasons)
        assert "AFFINITY_NOT_AS_REQUESTED" not in codes(reasons)
        assert verdict == VERDICT_UNKNOWN


class TestTheoreticalPeak:
    def test_unknown_peak_is_info_only_and_still_passes(self):
        reasons, verdict = evaluate(
            clean_input(peak_known=False, peak_gb_s=None)
        )
        assert "THEORETICAL_PEAK_UNKNOWN" in codes(reasons)
        assert verdict == VERDICT_PASS

    def test_a_figure_above_theoretical_is_invalidating(self):
        reasons, verdict = evaluate(clean_input(achieved_gb_s=90.0, peak_gb_s=76.8))
        assert "FRACTION_ABOVE_THEORETICAL" in codes(reasons)
        assert verdict == VERDICT_INVALID

    def test_a_figure_just_below_theoretical_is_fine(self):
        reasons, verdict = evaluate(clean_input(achieved_gb_s=76.7, peak_gb_s=76.8))
        assert "FRACTION_ABOVE_THEORETICAL" not in codes(reasons)
        assert verdict == VERDICT_PASS


class TestTimingQuality:
    def test_a_short_run_is_a_warning(self):
        reasons, verdict = evaluate(clean_input(best_seconds=0.001))
        assert "RUN_TOO_SHORT" in codes(reasons)
        assert verdict == VERDICT_PASS

    def test_a_coarse_clock_relative_to_the_run_is_a_warning(self):
        reasons, _ = evaluate(clean_input(best_seconds=0.25, timer_resolution_s=0.001))
        assert "TIMER_RESOLUTION_COARSE" in codes(reasons)

    def test_a_fine_clock_adds_nothing(self):
        reasons, _ = evaluate(clean_input(best_seconds=0.25, timer_resolution_s=1e-9))
        assert "TIMER_RESOLUTION_COARSE" not in codes(reasons)

    def test_high_spread_between_repeats_is_a_warning(self):
        reasons, verdict = evaluate(clean_input(max_spread_fraction=0.5))
        assert "HIGH_RUN_VARIANCE" in codes(reasons)
        assert verdict == VERDICT_PASS

    def test_the_spread_tolerance_is_configurable(self):
        reasons, _ = evaluate(
            clean_input(max_spread_fraction=0.5, spread_tolerance=0.9)
        )
        assert "HIGH_RUN_VARIANCE" not in codes(reasons)


class TestCodePathLabelling:
    def test_the_numpy_path_is_always_labelled(self):
        reasons, verdict = evaluate(clean_input(code_path="numpy"))
        assert "NUMPY_PATH_OVERHEAD" in codes(reasons)
        assert verdict == VERDICT_PASS
        detail = next(r["detail"] for r in reasons if r["code"] == "NUMPY_PATH_OVERHEAD")
        assert "lower bound" in detail

    def test_the_c_path_adds_no_path_reason(self):
        reasons, _ = evaluate(clean_input(code_path="c"))
        assert "NUMPY_PATH_OVERHEAD" not in codes(reasons)
        assert "C_PATH_UNAVAILABLE" not in codes(reasons)

    def test_a_fallback_records_why_the_c_path_was_not_used(self):
        reasons, _ = evaluate(
            clean_input(code_path="numpy", c_unavailable_reason="no C compiler was found")
        )
        assert "C_PATH_UNAVAILABLE" in codes(reasons)
        detail = next(r["detail"] for r in reasons if r["code"] == "C_PATH_UNAVAILABLE")
        assert "no C compiler" in detail


class TestPlatformAndTopology:
    def test_an_unsupported_platform_short_circuits_everything(self):
        reasons, verdict = evaluate(
            clean_input(
                platform_supported=False,
                platform_detail="nothing was measured",
                satisfies_margin=False,
                busy_fraction=0.99,
            )
        )
        assert codes(reasons) == {"PLATFORM_UNSUPPORTED"}
        assert verdict == VERDICT_UNSUPPORTED

    def test_an_assumed_single_cluster_is_recorded_as_info(self):
        reasons, verdict = evaluate(
            clean_input(clusters_detected=1, cluster_source="assumed-single-cluster")
        )
        assert "PER_CLUSTER_UNAVAILABLE" in codes(reasons)
        assert verdict == VERDICT_PASS

    def test_a_real_single_cluster_source_is_not_flagged(self):
        reasons, _ = evaluate(
            clean_input(clusters_detected=1, cluster_source="/proc/cpuinfo CPU part")
        )
        assert "PER_CLUSTER_UNAVAILABLE" not in codes(reasons)


class TestVerdictPrecedence:
    def test_invalidating_beats_confidence_blocking(self):
        reasons, verdict = evaluate(
            clean_input(satisfies_margin=False, governor=None, busy_fraction=None)
        )
        assert verdict == VERDICT_INVALID

    def test_unsupported_beats_invalid(self):
        reasons = [
            {"code": "PLATFORM_UNSUPPORTED", "severity": SEVERITY_INVALIDATING, "detail": ""},
            {"code": "COMPETING_LOAD", "severity": SEVERITY_INVALIDATING, "detail": ""},
        ]
        assert verdict_from_reasons(reasons) == VERDICT_UNSUPPORTED

    def test_info_only_still_passes(self):
        reasons = [
            {"code": "THEORETICAL_PEAK_UNKNOWN", "severity": "info", "detail": ""}
        ]
        assert verdict_from_reasons(reasons) == VERDICT_PASS

    def test_no_reasons_passes(self):
        assert verdict_from_reasons([]) == VERDICT_PASS


class TestVocabularyIntegrity:
    def test_every_code_emitted_is_in_the_registry(self):
        """A typo in a reason code must fail the suite, not reach output."""
        scenarios = [
            clean_input(),
            clean_input(platform_supported=False),
            clean_input(satisfies_margin=False, insufficient_memory=True),
            clean_input(llc_known=False),
            clean_input(pinning_supported=False),
            clean_input(affinity_as_requested=False),
            clean_input(busy_fraction=None),
            clean_input(busy_fraction=0.9),
            clean_input(throttle_counters_readable=False),
            clean_input(thermal_throttled=True, quota_throttled=True),
            clean_input(swap_delta=1, major_fault_delta=1),
            clean_input(governor=None),
            clean_input(governor="powersave", governor_changed=True),
            clean_input(governor_restore_verified=False),
            clean_input(peak_known=False),
            clean_input(achieved_gb_s=999.0),
            clean_input(best_seconds=1e-6, max_spread_fraction=1.0),
            clean_input(code_path="numpy", c_unavailable_reason="x"),
            clean_input(clusters_detected=1, cluster_source="assumed-single-cluster"),
        ]
        emitted = set()
        for scenario in scenarios:
            for item in build_reasons(scenario):
                emitted.add(item["code"])
                assert item["severity"] in SEVERITIES
                assert item["detail"]
                assert set(item) == {"code", "severity", "detail"}
        unknown = emitted - set(REASON_CODES)
        assert unknown == set(), "codes not in the registry: %s" % sorted(unknown)

    def test_the_registry_only_uses_known_severities(self):
        for code, (severity, meaning) in REASON_CODES.items():
            assert severity in SEVERITIES, code
            assert meaning, code
            assert meaning[0].isupper() or meaning.startswith("memceiling"), code
            assert meaning.endswith("."), code

    def test_confidence_blocking_codes_exist_and_are_warnings(self):
        for code in CONFIDENCE_BLOCKING:
            assert code in REASON_CODES
            assert REASON_CODES[code][0] == "warning"

    def test_no_reason_detail_contains_an_em_dash(self):
        """A hard project rule, enforced on generated prose too."""
        em_dash, en_dash = chr(0x2014), chr(0x2013)
        for scenario in (clean_input(), clean_input(satisfies_margin=False)):
            for item in build_reasons(scenario):
                assert em_dash not in item["detail"]
                assert en_dash not in item["detail"]
        for _, meaning in REASON_CODES.values():
            assert em_dash not in meaning
            assert en_dash not in meaning
