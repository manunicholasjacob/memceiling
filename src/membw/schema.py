"""Stable identifiers for the membw output envelope.

Nothing in this module measures anything. It exists so that consumers can
import the vocabulary without importing the measurement machinery, and so
that the reason codes documented in README.md have exactly one definition.

The outer envelope is shared with sibling tools (see README, "Related
tools"). membw does not own the envelope's verdict vocabulary; it conforms
to it. membw owns only the codes in REASON_CODES below and everything
under the envelope's "measurement" key.
"""

from __future__ import annotations

SCHEMA_ID = "measurement-provenance/1"
TOOL_NAME = "membw"

# Severities, as used in envelope["reasons"][i]["severity"].
SEVERITY_INVALIDATING = "invalidating"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

SEVERITIES = (SEVERITY_INVALIDATING, SEVERITY_WARNING, SEVERITY_INFO)

# Verdicts, as used in envelope["verdict"].
VERDICT_PASS = "PASS"
VERDICT_INVALID = "INVALID"
VERDICT_UNSUPPORTED = "UNSUPPORTED"
VERDICT_UNKNOWN = "UNKNOWN"

VERDICTS = (VERDICT_PASS, VERDICT_INVALID, VERDICT_UNSUPPORTED, VERDICT_UNKNOWN)

# Code -> (default severity, one-line meaning).
#
# A code's severity is a default, not a law: a few codes are promoted or
# demoted at runtime (for example THEORETICAL_PEAK_UNKNOWN stays info, but
# FRACTION_ABOVE_THEORETICAL is always invalidating). Anything that changes
# severity at runtime does so in membw.validity and says why there.
REASON_CODES = {
    # Working set versus last-level cache.
    "WORKING_SET_BELOW_LLC": (
        SEVERITY_INVALIDATING,
        "Per-array working set did not exceed the last-level cache by the "
        "required margin, so the figure may be a cache bandwidth.",
    ),
    "LLC_SIZE_UNKNOWN": (
        SEVERITY_WARNING,
        "Last-level cache size could not be read, so the working-set margin "
        "could not be checked.",
    ),
    "MEMORY_INSUFFICIENT": (
        SEVERITY_INVALIDATING,
        "Available memory was too small to allocate buffers that clear the "
        "last-level cache by the required margin.",
    ),
    # Competing load.
    "COMPETING_LOAD": (
        SEVERITY_INVALIDATING,
        "Another workload was using the target CPUs before the run.",
    ),
    "LOAD_UNREADABLE": (
        SEVERITY_WARNING,
        "CPU busy fraction could not be read, so competing load could not be "
        "ruled out.",
    ),
    # Throttling during the run.
    "THERMAL_THROTTLE_DURING_RUN": (
        SEVERITY_INVALIDATING,
        "A thermal throttle counter advanced during the run.",
    ),
    "QUOTA_THROTTLE_DURING_RUN": (
        SEVERITY_INVALIDATING,
        "A cgroup CPU quota throttle counter advanced during the run.",
    ),
    "THROTTLE_COUNTERS_UNREADABLE": (
        SEVERITY_WARNING,
        "No thermal or quota throttle counter could be read, so throttling "
        "during the run could not be ruled out.",
    ),
    "SWAP_ACTIVE_DURING_RUN": (
        SEVERITY_INVALIDATING,
        "Pages were swapped in or out during the run.",
    ),
    "MAJOR_FAULTS_DURING_RUN": (
        SEVERITY_WARNING,
        "The process took major page faults during the run.",
    ),
    # Governor and frequency policy.
    "GOVERNOR_CHANGED_DURING_RUN": (
        SEVERITY_INVALIDATING,
        "The CPU frequency governor was not the same before and after the run.",
    ),
    "GOVERNOR_NOT_PERFORMANCE": (
        SEVERITY_WARNING,
        "The CPU frequency governor was not 'performance', so the figure is a "
        "governor-dependent number.",
    ),
    "GOVERNOR_UNKNOWN": (
        SEVERITY_WARNING,
        "The CPU frequency governor could not be read.",
    ),
    "GOVERNOR_RESTORE_FAILED": (
        SEVERITY_INVALIDATING,
        "membw changed the governor on request and could not verify that the "
        "original value was restored.",
    ),
    # Affinity.
    "AFFINITY_NOT_AS_REQUESTED": (
        SEVERITY_INVALIDATING,
        "The CPU affinity mask actually in effect differed from the one "
        "requested.",
    ),
    "AFFINITY_UNSUPPORTED": (
        SEVERITY_WARNING,
        "Thread pinning is not available on this platform, so the threads may "
        "have migrated during the run.",
    ),
    # Theoretical peak.
    "THEORETICAL_PEAK_UNKNOWN": (
        SEVERITY_INFO,
        "Theoretical peak bandwidth could not be derived from readable "
        "sources, so the achieved fraction is not reported.",
    ),
    "FRACTION_ABOVE_THEORETICAL": (
        SEVERITY_INVALIDATING,
        "Achieved bandwidth exceeded the derived theoretical peak, so either "
        "the measurement or the memory configuration read is wrong.",
    ),
    # Timing and repeatability.
    "RUN_TOO_SHORT": (
        SEVERITY_WARNING,
        "The timed region was short relative to the clock resolution.",
    ),
    "TIMER_RESOLUTION_COARSE": (
        SEVERITY_WARNING,
        "The monotonic clock resolution is coarse relative to the timed "
        "region.",
    ),
    "HIGH_RUN_VARIANCE": (
        SEVERITY_WARNING,
        "Repeat timings of the same kernel disagreed by more than the "
        "tolerated spread.",
    ),
    # Code path.
    "NUMPY_PATH_OVERHEAD": (
        SEVERITY_INFO,
        "The number was produced by the numpy path, whose per-call overhead "
        "and extra array passes make it a lower bound on a fused C kernel.",
    ),
    "C_PATH_UNAVAILABLE": (
        SEVERITY_INFO,
        "The compiled C kernel was not available, so the numpy path was used.",
    ),
    # Platform.
    "PLATFORM_UNSUPPORTED": (
        SEVERITY_INVALIDATING,
        "membw cannot measure on this platform.",
    ),
    "PER_CLUSTER_UNAVAILABLE": (
        SEVERITY_INFO,
        "CPU cluster topology could not be determined, so only an aggregate "
        "figure is reported.",
    ),
}

# Warnings that mean "membw could not establish validity", as opposed to
# "membw established that something was off". A run carrying one of these and
# no invalidating reason gets verdict UNKNOWN rather than PASS, because a
# clean-looking report with an unread source behind it is the failure mode
# this tool exists to prevent.
CONFIDENCE_BLOCKING = frozenset(
    {
        "LLC_SIZE_UNKNOWN",
        "LOAD_UNREADABLE",
        "THROTTLE_COUNTERS_UNREADABLE",
        "AFFINITY_UNSUPPORTED",
        "GOVERNOR_UNKNOWN",
    }
)


def severity_of(code: str) -> str:
    """Return the default severity for a reason code.

    Raises KeyError for an unknown code, deliberately: a typo in a code
    should fail loudly rather than appear in output as a new vocabulary item.
    """
    return REASON_CODES[code][0]


def description_of(code: str) -> str:
    """Return the one-line meaning of a reason code."""
    return REASON_CODES[code][1]
