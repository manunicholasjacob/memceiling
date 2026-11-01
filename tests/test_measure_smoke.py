"""End-to-end tests that do not depend on how fast the machine is.

These run real measurements, but deliberately tiny ones, and assert only on
structure, labelling and refusal behaviour. The only numeric assertions are
that a bandwidth is positive and finite, which is true of every machine.
"""

import json
import math

import pytest

from memceiling import MeasureOptions, measure
from memceiling.kernels import PATH_C, PATH_NUMPY
from memceiling.schema import SEVERITIES, VERDICTS

MIB = 1024 * 1024


@pytest.fixture(scope="module")
def tiny_numpy_run():
    """A deliberately small numpy-path run, for structural assertions."""
    return measure(
        MeasureOptions(
            cpus=[0],
            code_path=PATH_NUMPY,
            reps=2,
            bytes_per_array=2 * MIB,
            min_run_seconds=0.01,
            per_cluster=False,
            allow_c_build=False,
        )
    )


class TestResultObject:
    def test_verdict_is_from_the_vocabulary(self, tiny_numpy_run):
        assert tiny_numpy_run.verdict in VERDICTS

    def test_envelope_is_json_serialisable_and_ascii(self, tiny_numpy_run):
        text = tiny_numpy_run.to_json()
        text.encode("ascii")
        assert json.loads(text)["schema"] == "measurement-provenance/1"

    def test_bandwidth_figures_are_positive_and_finite(self, tiny_numpy_run):
        for value in (
            tiny_numpy_run.read_gb_s,
            tiny_numpy_run.copy_gb_s,
            tiny_numpy_run.triad_gb_s,
            tiny_numpy_run.ceiling_gb_s,
        ):
            assert value is not None
            assert math.isfinite(value)
            assert value > 0

    def test_the_ceiling_is_the_largest_of_the_three_kernels(self, tiny_numpy_run):
        assert tiny_numpy_run.ceiling_gb_s == max(
            tiny_numpy_run.read_gb_s,
            tiny_numpy_run.copy_gb_s,
            tiny_numpy_run.triad_gb_s,
        )
        assert tiny_numpy_run.ceiling_kernel in ("read", "copy", "triad")

    def test_trustworthy_tracks_the_verdict(self, tiny_numpy_run):
        assert tiny_numpy_run.trustworthy == (tiny_numpy_run.verdict == "PASS")


class TestCodePathLabelling:
    def test_the_numpy_path_is_recorded_on_the_run_and_every_kernel(
        self, tiny_numpy_run
    ):
        measurement = tiny_numpy_run.envelope["measurement"]
        assert measurement["code_path"]["used"] == PATH_NUMPY
        assert tiny_numpy_run.code_path == PATH_NUMPY
        for kernel in measurement["aggregate"]["kernels"].values():
            assert kernel["code_path"] == PATH_NUMPY

    def test_the_numpy_triad_is_labelled_a_lower_bound(self, tiny_numpy_run):
        triad = tiny_numpy_run.envelope["measurement"]["aggregate"]["kernels"]["triad"]
        assert "lower bound" in triad["note"]
        assert "five" in triad["note"]

    def test_the_numpy_path_always_carries_its_overhead_reason(self, tiny_numpy_run):
        codes = {r["code"] for r in tiny_numpy_run.reasons}
        assert "NUMPY_PATH_OVERHEAD" in codes


class TestByteConventionsAreStated:
    def test_every_kernel_states_its_byte_convention(self, tiny_numpy_run):
        kernels = tiny_numpy_run.envelope["measurement"]["aggregate"]["kernels"]
        assert "one array read" in kernels["read"]["byte_convention"]
        assert "STREAM Copy" in kernels["copy"]["byte_convention"]
        assert "STREAM Triad" in kernels["triad"]["byte_convention"]

    def test_the_stream_multipliers_are_the_documented_ones(self, tiny_numpy_run):
        kernels = tiny_numpy_run.envelope["measurement"]["aggregate"]["kernels"]
        read_per_pass = kernels["read"]["bytes_per_repeat"] / kernels["read"][
            "inner_passes_per_repeat"
        ]
        copy_per_pass = kernels["copy"]["bytes_per_repeat"] / kernels["copy"][
            "inner_passes_per_repeat"
        ]
        triad_per_pass = kernels["triad"]["bytes_per_repeat"] / kernels["triad"][
            "inner_passes_per_repeat"
        ]
        assert copy_per_pass == 2 * read_per_pass
        assert triad_per_pass == 3 * read_per_pass


class TestProvenanceIsPresent:
    def test_the_affinity_block_reports_requested_and_observed(self, tiny_numpy_run):
        affinity = tiny_numpy_run.envelope["measurement"]["aggregate"]["affinity"]
        assert affinity["requested_cpu_list"] == "0"
        assert "every_thread_as_requested" in affinity
        assert "pinning_supported" in affinity
        assert isinstance(affinity["per_thread"], list)

    def test_the_theoretical_peak_block_always_states_the_formula(self, tiny_numpy_run):
        peak = tiny_numpy_run.envelope["measurement"]["theoretical_peak"]
        assert peak["formula"].startswith("peak_GB_s =")
        assert peak["status"] in ("KNOWN", "UNKNOWN")
        if peak["status"] == "UNKNOWN":
            assert peak["peak_gb_s"] is None

    def test_the_buffer_block_states_the_margin_decision(self, tiny_numpy_run):
        buffers = tiny_numpy_run.envelope["measurement"]["aggregate"]["buffers"]
        assert "satisfies_margin" in buffers
        assert "rationale" in buffers and buffers["rationale"]
        assert buffers["llc_margin"] > 0

    def test_the_conditions_block_is_present(self, tiny_numpy_run):
        conditions = tiny_numpy_run.envelope["measurement"]["conditions"]
        for key in (
            "governor",
            "pre_run_busy_fraction",
            "throttle_counter_sources",
            "throttle_deltas",
        ):
            assert key in conditions

    def test_the_thresholds_used_are_recorded(self, tiny_numpy_run):
        thresholds = tiny_numpy_run.envelope["measurement"]["thresholds"]
        for key in ("llc_margin", "busy_threshold", "spread_tolerance"):
            assert key in thresholds

    def test_unreadable_sources_are_listed_not_silently_dropped(self, tiny_numpy_run):
        assert isinstance(tiny_numpy_run.envelope["unreadable"], list)

    def test_every_reason_is_well_formed(self, tiny_numpy_run):
        for item in tiny_numpy_run.reasons:
            assert set(item) == {"code", "severity", "detail"}
            assert item["severity"] in SEVERITIES
            assert item["detail"].endswith(".")


class TestRefusalEndToEnd:
    def test_a_working_set_inside_the_cache_is_refused(self):
        """A margin no machine can satisfy must not come back PASS."""
        result = measure(
            MeasureOptions(
                cpus=[0],
                code_path=PATH_NUMPY,
                reps=1,
                bytes_per_array=2 * MIB,
                llc_margin=100000.0,
                min_run_seconds=0.01,
                per_cluster=False,
                allow_c_build=False,
            )
        )
        assert result.verdict != "PASS"
        assert not result.trustworthy
        codes = {r["code"] for r in result.reasons}
        # Either the cache size was read and the margin fails, or it could
        # not be read at all. Both are refusals, neither is a PASS.
        assert codes & {"WORKING_SET_BELOW_LLC", "LLC_SIZE_UNKNOWN"}

    def test_demanding_a_missing_c_path_is_unsupported_not_a_silent_downgrade(self):
        result = measure(
            MeasureOptions(
                cpus=[0],
                code_path=PATH_C,
                c_library="/nonexistent/path/to/memceiling_ckernels.so",
                allow_c_build=False,
                reps=1,
                bytes_per_array=2 * MIB,
                per_cluster=False,
            )
        )
        assert result.verdict == "UNSUPPORTED"
        assert result.envelope["measurement"]["code_path"]["used"] is None
        assert result.envelope["measurement"]["code_path"]["c_unavailable_reason"]

    def test_a_competing_load_threshold_of_zero_refuses(self):
        result = measure(
            MeasureOptions(
                cpus=[0],
                code_path=PATH_NUMPY,
                reps=1,
                bytes_per_array=2 * MIB,
                busy_threshold=-1.0,
                min_run_seconds=0.01,
                per_cluster=False,
                allow_c_build=False,
            )
        )
        codes = {r["code"] for r in result.reasons}
        assert codes & {"COMPETING_LOAD", "LOAD_UNREADABLE"}
        assert result.verdict != "PASS"


class TestOptionsAreRecorded:
    def test_the_options_used_are_echoed_into_the_record(self, tiny_numpy_run):
        options = tiny_numpy_run.envelope["measurement"]["options"]
        assert options["code_path_requested"] == PATH_NUMPY
        assert options["repeats"] == 2
        assert options["bytes_per_array_requested"] == 2 * MIB
        assert options["cpus_requested"] == "0"
        assert options["set_governor"] is None

    def test_memceiling_does_not_change_the_governor_by_default(self, tiny_numpy_run):
        conditions = tiny_numpy_run.envelope["measurement"]["conditions"]
        assert conditions["governor_change_requested"] is None
