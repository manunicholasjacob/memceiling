"""Decide whether a bandwidth number is trustworthy, and say why not.

This module is deliberately free of I/O. It takes a ValidityInput of plain
values that the readers in topology.py, conditions.py and kernels.py have
already gathered, and returns the envelope's `reasons` list and `verdict`.
That is what makes the refusal behaviour testable without a particular
machine: a test can hand it a working set below the cache, or a throttle
counter that advanced, and assert on the verdict.

The verdict rule, in order:

  1. PLATFORM_UNSUPPORTED present               -> UNSUPPORTED
  2. any reason of severity "invalidating"      -> INVALID
  3. any confidence-blocking warning present    -> UNKNOWN
  4. otherwise                                  -> PASS

Step 3 is the part that distinguishes memceiling from a tool that prints a
number and a blank line. A warning that says "memceiling could not read the
cache size" is not a clean run; it is an unverified one, and it must not
come out as PASS.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import (
    CONFIDENCE_BLOCKING,
    SEVERITY_INFO,
    SEVERITY_INVALIDATING,
    SEVERITY_WARNING,
    VERDICT_INVALID,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    VERDICT_UNSUPPORTED,
    severity_of,
)

# Defaults for the thresholds the rules use. Each is exposed on the CLI so an
# operator can see and change it, and each is recorded in the output so a
# consumer knows which threshold a verdict was reached under.
DEFAULT_BUSY_THRESHOLD = 0.10
DEFAULT_SPREAD_TOLERANCE = 0.10
DEFAULT_MIN_RUN_SECONDS = 0.05
DEFAULT_TIMER_RESOLUTION_RATIO = 1000.0


def reason(code: str, detail: str, severity: str | None = None) -> dict:
    """Build one entry of the envelope's `reasons` list."""
    return {
        "code": code,
        "severity": severity or severity_of(code),
        "detail": detail,
    }


@dataclass
class ValidityInput:
    """Everything the rules need, as plain values already read elsewhere."""

    platform_supported: bool = True
    platform_detail: str = ""

    # Working set versus last-level cache.
    llc_known: bool = True
    llc_total_bytes: int | None = None
    bytes_per_array: int | None = None
    required_bytes_per_array: int | None = None
    satisfies_margin: bool = True
    insufficient_memory: bool = False
    llc_margin: float = 4.0

    # Affinity.
    pinning_supported: bool = True
    affinity_as_requested: bool = True
    affinity_detail: str = ""

    # Competing load.
    busy_fraction: float | None = None
    busy_source: str | None = None
    busy_threshold: float = DEFAULT_BUSY_THRESHOLD

    # Governor.
    governor: str | None = None
    governor_changed: bool = False
    governor_before: str | None = None
    governor_after: str | None = None
    governor_restore_verified: bool | None = None

    # Throttling and memory pressure during the run.
    throttle_counters_readable: bool = True
    thermal_throttled: bool = False
    quota_throttled: bool = False
    throttle_deltas: dict = field(default_factory=dict)
    swap_delta: int | None = None
    major_fault_delta: int | None = None

    # Theoretical peak and the achieved figure.
    peak_known: bool = True
    peak_gb_s: float | None = None
    achieved_gb_s: float | None = None
    achieved_kernel: str | None = None

    # Code path.
    code_path: str = "c"
    c_unavailable_reason: str | None = None

    # Timing quality.
    timer_resolution_s: float | None = None
    best_seconds: float | None = None
    max_spread_fraction: float | None = None
    spread_tolerance: float = DEFAULT_SPREAD_TOLERANCE
    min_run_seconds: float = DEFAULT_MIN_RUN_SECONDS

    # Topology.
    clusters_detected: int = 1
    cluster_source: str = ""

    errors: list = field(default_factory=list)


def build_reasons(data: ValidityInput) -> list:
    """Turn a ValidityInput into the envelope's `reasons` list."""
    reasons: list = []

    if not data.platform_supported:
        reasons.append(
            reason(
                "PLATFORM_UNSUPPORTED",
                data.platform_detail or "memceiling cannot measure on this platform.",
            )
        )
        return reasons

    # --- working set versus last-level cache -------------------------------
    if data.insufficient_memory:
        reasons.append(
            reason(
                "MEMORY_INSUFFICIENT",
                "Available memory allowed only %s bytes per array, which is "
                "below the one-million-element floor memceiling takes from STREAM's "
                "run rules."
                % (data.bytes_per_array,),
            )
        )
    if not data.llc_known:
        reasons.append(
            reason(
                "LLC_SIZE_UNKNOWN",
                "The last-level cache size could not be read, so memceiling sized "
                "the arrays to a fixed fallback floor and could not verify "
                "that the working set clears the cache.",
            )
        )
    elif not data.satisfies_margin:
        reasons.append(
            reason(
                "WORKING_SET_BELOW_LLC",
                "Each array was %s bytes, below the %s bytes required by a "
                "%.1fx margin over the %s byte summed last-level cache, so "
                "the figure may be a cache bandwidth rather than a memory "
                "bandwidth."
                % (
                    data.bytes_per_array,
                    data.required_bytes_per_array,
                    data.llc_margin,
                    data.llc_total_bytes,
                ),
            )
        )

    # --- affinity -----------------------------------------------------------
    if not data.pinning_supported:
        reasons.append(
            reason(
                "AFFINITY_UNSUPPORTED",
                data.affinity_detail
                or "This platform offers no thread pinning mechanism memceiling can "
                "use, so a thread may have migrated between cores during the "
                "run.",
            )
        )
    elif not data.affinity_as_requested:
        reasons.append(
            reason(
                "AFFINITY_NOT_AS_REQUESTED",
                data.affinity_detail
                or "The CPU affinity mask read back after pinning differed from "
                "the mask requested, so the threads did not run where memceiling "
                "intended.",
            )
        )

    # --- competing load -----------------------------------------------------
    if data.busy_fraction is None:
        reasons.append(
            reason(
                "LOAD_UNREADABLE",
                "CPU busy fraction could not be read before the run, so a "
                "competing workload could not be ruled out.",
            )
        )
    elif data.busy_fraction > data.busy_threshold:
        reasons.append(
            reason(
                "COMPETING_LOAD",
                "The target CPUs were %.1f%% busy before memceiling started, above "
                "the %.1f%% threshold, measured from %s."
                % (
                    data.busy_fraction * 100.0,
                    data.busy_threshold * 100.0,
                    data.busy_source or "an unnamed source",
                ),
            )
        )

    # --- throttling and memory pressure ------------------------------------
    if not data.throttle_counters_readable:
        reasons.append(
            reason(
                "THROTTLE_COUNTERS_UNREADABLE",
                "No thermal or CPU-quota throttle counter was readable, so "
                "throttling during the run could not be ruled out.",
            )
        )
    if data.thermal_throttled:
        advanced = sorted(
            k for k, v in (data.throttle_deltas or {}).items()
            if v and not k.startswith("quota:")
        )
        reasons.append(
            reason(
                "THERMAL_THROTTLE_DURING_RUN",
                "A thermal throttle counter advanced during the run (%s), so "
                "part of the run executed below the clock the rest of it used."
                % (", ".join(advanced) or "counter name unavailable"),
            )
        )
    if data.quota_throttled:
        advanced = sorted(
            k for k, v in (data.throttle_deltas or {}).items()
            if v and k.startswith("quota:")
        )
        reasons.append(
            reason(
                "QUOTA_THROTTLE_DURING_RUN",
                "A cgroup CPU quota throttle counter advanced during the run "
                "(%s), so the run was descheduled by its own quota."
                % (", ".join(advanced) or "counter name unavailable"),
            )
        )
    if data.swap_delta:
        reasons.append(
            reason(
                "SWAP_ACTIVE_DURING_RUN",
                "%d swap page transfers happened during the run, so some of "
                "the measured traffic was not DRAM traffic."
                % data.swap_delta,
            )
        )
    if data.major_fault_delta:
        reasons.append(
            reason(
                "MAJOR_FAULTS_DURING_RUN",
                "The process took %d major page faults during the run."
                % data.major_fault_delta,
            )
        )

    # --- governor -----------------------------------------------------------
    if data.governor is None:
        reasons.append(
            reason(
                "GOVERNOR_UNKNOWN",
                "The CPU frequency governor could not be read, so the clock "
                "policy this figure was taken under is not recorded.",
            )
        )
    else:
        if data.governor_changed:
            reasons.append(
                reason(
                    "GOVERNOR_CHANGED_DURING_RUN",
                    "The governor was %r before the run and %r after it, so "
                    "the run spanned a change of clock policy."
                    % (data.governor_before, data.governor_after),
                )
            )
        if data.governor != "performance" and not data.governor.startswith("mixed:"):
            reasons.append(
                reason(
                    "GOVERNOR_NOT_PERFORMANCE",
                    "The governor was %r, not 'performance', so this figure is "
                    "specific to that clock policy and is not the machine's "
                    "best achievable bandwidth." % data.governor,
                )
            )
        elif data.governor.startswith("mixed:"):
            reasons.append(
                reason(
                    "GOVERNOR_NOT_PERFORMANCE",
                    "The target CPUs did not share one governor (%s), so the "
                    "figure spans more than one clock policy." % data.governor,
                )
            )
    if data.governor_restore_verified is False:
        reasons.append(
            reason(
                "GOVERNOR_RESTORE_FAILED",
                "memceiling changed the governor on request and could not verify "
                "that the original value was restored. The machine may have "
                "been left in a changed power state; check it.",
            )
        )

    # --- theoretical peak ---------------------------------------------------
    if not data.peak_known:
        reasons.append(
            reason(
                "THEORETICAL_PEAK_UNKNOWN",
                "The memory configuration could not be read, so the "
                "theoretical peak and the achieved fraction are omitted "
                "rather than guessed.",
            )
        )
    elif (
        data.peak_gb_s
        and data.achieved_gb_s
        and data.achieved_gb_s > data.peak_gb_s
    ):
        reasons.append(
            reason(
                "FRACTION_ABOVE_THEORETICAL",
                "The achieved %s figure of %.2f GB/s exceeds the derived "
                "theoretical peak of %.2f GB/s. One of the two is wrong: "
                "either the working set was served from cache, or the memory "
                "configuration was misread."
                % (
                    data.achieved_kernel or "bandwidth",
                    data.achieved_gb_s,
                    data.peak_gb_s,
                ),
            )
        )

    # --- timing quality -----------------------------------------------------
    if data.best_seconds is not None:
        if data.best_seconds < data.min_run_seconds:
            reasons.append(
                reason(
                    "RUN_TOO_SHORT",
                    "The fastest timed repetition took %.6f s, below the "
                    "%.3f s memceiling treats as the minimum for a stable figure."
                    % (data.best_seconds, data.min_run_seconds),
                )
            )
        if (
            data.timer_resolution_s
            and data.best_seconds
            < data.timer_resolution_s * DEFAULT_TIMER_RESOLUTION_RATIO
        ):
            reasons.append(
                reason(
                    "TIMER_RESOLUTION_COARSE",
                    "The fastest timed repetition took %.6f s, less than %d "
                    "times the monotonic clock resolution of %.9f s."
                    % (
                        data.best_seconds,
                        int(DEFAULT_TIMER_RESOLUTION_RATIO),
                        data.timer_resolution_s,
                    ),
                )
            )
    if (
        data.max_spread_fraction is not None
        and data.max_spread_fraction > data.spread_tolerance
    ):
        reasons.append(
            reason(
                "HIGH_RUN_VARIANCE",
                "Repeat timings of the same kernel spread by %.1f%%, above the "
                "%.1f%% tolerance, so the machine was not in a steady state."
                % (
                    data.max_spread_fraction * 100.0,
                    data.spread_tolerance * 100.0,
                ),
            )
        )

    # --- code path ----------------------------------------------------------
    if data.code_path == "numpy":
        reasons.append(
            reason(
                "NUMPY_PATH_OVERHEAD",
                "This figure came from the numpy path. Its triad is two ufunc "
                "passes rather than one fused pass, and every kernel carries "
                "numpy per-call overhead, so treat the figure as a lower "
                "bound on what a compiled kernel reaches on this machine.",
            )
        )
        if data.c_unavailable_reason:
            reasons.append(
                reason(
                    "C_PATH_UNAVAILABLE",
                    "The compiled path was not used: %s"
                    % data.c_unavailable_reason,
                )
            )

    # --- topology -----------------------------------------------------------
    if data.clusters_detected <= 1 and data.cluster_source == "assumed-single-cluster":
        reasons.append(
            reason(
                "PER_CLUSTER_UNAVAILABLE",
                "No CPU cluster topology could be read, so memceiling assumed one "
                "cluster. On a heterogeneous SoC that assumption makes the "
                "aggregate figure hard to interpret.",
            )
        )

    return reasons


def verdict_from_reasons(reasons) -> str:
    """Apply the verdict rule to a reasons list."""
    codes = {r.get("code") for r in reasons}
    if "PLATFORM_UNSUPPORTED" in codes:
        return VERDICT_UNSUPPORTED
    if any(r.get("severity") == SEVERITY_INVALIDATING for r in reasons):
        return VERDICT_INVALID
    if codes & CONFIDENCE_BLOCKING:
        return VERDICT_UNKNOWN
    return VERDICT_PASS


def evaluate(data: ValidityInput) -> tuple:
    """Convenience wrapper returning (reasons, verdict)."""
    reasons = build_reasons(data)
    return reasons, verdict_from_reasons(reasons)


__all__ = [
    "DEFAULT_BUSY_THRESHOLD",
    "DEFAULT_MIN_RUN_SECONDS",
    "DEFAULT_SPREAD_TOLERANCE",
    "SEVERITY_INFO",
    "SEVERITY_INVALIDATING",
    "SEVERITY_WARNING",
    "VERDICT_INVALID",
    "VERDICT_PASS",
    "VERDICT_UNKNOWN",
    "VERDICT_UNSUPPORTED",
    "ValidityInput",
    "build_reasons",
    "evaluate",
    "reason",
    "verdict_from_reasons",
]
