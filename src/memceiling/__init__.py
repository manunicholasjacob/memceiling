"""memceiling: measure a machine's achieved memory bandwidth ceiling, with a record.

The small stable API, which the README documents and which a dependent may
rely on across 0.1.x:

    measure(options=None) -> Result        take a measurement
    MeasureOptions                         what a caller can change
    Result                                 .envelope, .verdict, .ceiling_gb_s

    detect_topology()                      CPUs, clusters, LLC, RAM
    derive_theoretical_peak()              the denominator, or UNKNOWN
    plan_buffers()                         array sizing and the margin check
    build_reasons() / verdict_from_reasons()   the refusal rules
    load_c_kernels() / build_c_kernels()   the optional compiled path

    SCHEMA_ID, REASON_CODES, VERDICTS      the output vocabulary

Typical use as a roofline denominator:

    from memceiling import measure
    result = measure()
    if not result.trustworthy:
        raise SystemExit(
            "refusing to plot a roofline: %s" % result.verdict
        )
    peak = result.ceiling_gb_s
"""

from __future__ import annotations

from .affinity import AffinityResult, pin_current_thread, pinning_supported
from .cbuild import BuildReport, build_c_kernels, load_c_kernels
from .conditions import measure_competing_load, sample_conditions
from .kernels import (
    DEFAULT_LLC_MARGIN,
    PATH_C,
    PATH_NUMPY,
    BufferPlan,
    plan_buffers,
)
from .run import MeasureOptions, Result, measure
from .schema import (
    REASON_CODES,
    SCHEMA_ID,
    SEVERITIES,
    TOOL_NAME,
    VERDICTS,
    description_of,
    severity_of,
)
from .theoretical import FORMULA, TheoreticalPeak, derive_theoretical_peak
from .topology import Topology, detect_topology, format_cpu_list
from .validity import ValidityInput, build_reasons, verdict_from_reasons
from .version import __version__

__all__ = [
    "AffinityResult",
    "BufferPlan",
    "BuildReport",
    "DEFAULT_LLC_MARGIN",
    "FORMULA",
    "MeasureOptions",
    "PATH_C",
    "PATH_NUMPY",
    "REASON_CODES",
    "Result",
    "SCHEMA_ID",
    "SEVERITIES",
    "TOOL_NAME",
    "TheoreticalPeak",
    "Topology",
    "VERDICTS",
    "ValidityInput",
    "__version__",
    "build_c_kernels",
    "build_reasons",
    "derive_theoretical_peak",
    "description_of",
    "detect_topology",
    "format_cpu_list",
    "load_c_kernels",
    "measure",
    "measure_competing_load",
    "pin_current_thread",
    "pinning_supported",
    "plan_buffers",
    "sample_conditions",
    "severity_of",
    "verdict_from_reasons",
]
