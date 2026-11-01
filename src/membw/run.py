"""Orchestration: take a measurement and build the provenance envelope.

The public entry point is measure(). It is the whole library API that most
callers need:

    from membw import measure
    result = measure()
    if result.verdict == "PASS":
        denominator = result.ceiling_gb_s

Everything measure() reports it either read or measured in this process.
Nothing is filled in from a datasheet, a lookup table or a previous run.
"""

from __future__ import annotations

import datetime
import json
import platform
import sys
import time
from dataclasses import dataclass, field

from . import conditions as _conditions
from . import kernels as _kernels
from . import schema as _schema
from . import theoretical as _theoretical
from . import topology as _topology
from . import validity as _validity
from .cbuild import load_c_kernels
from .version import __version__

CODE_PATH_AUTO = "auto"


@dataclass
class MeasureOptions:
    """Everything a caller can change about a run.

    Defaults are the ones membw considers defensible: one thread per CPU,
    STREAM's 4x cache margin, the compiled path when it is available, and no
    change to the machine's power state.
    """

    cpus: list | None = None
    per_cluster: bool | None = None
    code_path: str = CODE_PATH_AUTO
    reps: int = 5
    threads: int | None = None
    llc_margin: float = _kernels.DEFAULT_LLC_MARGIN
    bytes_per_array: int | None = None
    memory_fraction: float = _kernels.DEFAULT_MEMORY_FRACTION
    memory_config: str | None = None
    include_strided: bool = False
    scalar: float = 3.0
    busy_threshold: float = _validity.DEFAULT_BUSY_THRESHOLD
    spread_tolerance: float = _validity.DEFAULT_SPREAD_TOLERANCE
    min_run_seconds: float = _validity.DEFAULT_MIN_RUN_SECONDS
    load_window_s: float = 0.4
    set_governor: str | None = None
    c_library: str | None = None
    allow_c_build: bool = True

    def as_dict(self) -> dict:
        return {
            "cpus_requested": (
                None if self.cpus is None else _topology.format_cpu_list(self.cpus)
            ),
            "per_cluster": self.per_cluster,
            "code_path_requested": self.code_path,
            "repeats": self.reps,
            "threads_requested": self.threads,
            "llc_margin": self.llc_margin,
            "bytes_per_array_requested": self.bytes_per_array,
            "memory_fraction": self.memory_fraction,
            "memory_config_override": self.memory_config,
            "include_strided": self.include_strided,
            "triad_scalar": self.scalar,
            "busy_threshold": self.busy_threshold,
            "spread_tolerance": self.spread_tolerance,
            "min_run_seconds": self.min_run_seconds,
            "load_window_s": self.load_window_s,
            "set_governor": self.set_governor,
        }


@dataclass
class Result:
    """A finished run: the envelope, plus the few fields callers reach for."""

    envelope: dict
    verdict: str
    reasons: list = field(default_factory=list)
    ceiling_gb_s: float | None = None
    ceiling_kernel: str | None = None
    read_gb_s: float | None = None
    copy_gb_s: float | None = None
    triad_gb_s: float | None = None
    theoretical_peak_gb_s: float | None = None
    fraction_of_theoretical: float | None = None
    code_path: str | None = None

    @property
    def trustworthy(self) -> bool:
        """True only when the verdict is PASS.

        A caller using membw as a roofline denominator should branch on this
        and refuse to plot rather than quietly use an INVALID figure.
        """
        return self.verdict == _schema.VERDICT_PASS

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.envelope, indent=indent, sort_keys=False)

    def to_envelope(self) -> dict:
        return self.envelope


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _platform_block() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _dedupe(items) -> list:
    seen = set()
    out = []
    for item in items:
        if item is None or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _gb_s(run, name):
    kernel = run.kernels.get(name) if run else None
    return None if kernel is None else kernel.gb_s


def _max_spread(run) -> float | None:
    if run is None:
        return None
    spreads = [
        k.spread_fraction for k in run.kernels.values() if k.spread_fraction is not None
    ]
    return max(spreads) if spreads else None


def _best_seconds(run) -> float | None:
    if run is None:
        return None
    values = [k.best_s for k in run.kernels.values() if k.best_s]
    return min(values) if values else None


def _ceiling(run) -> tuple:
    """The highest achieved figure in a run, and the kernel that produced it.

    membw calls this the achieved ceiling. It is deliberately the maximum
    over the kernels rather than the read figure alone, because on some
    memory systems Copy reaches higher than a pure read and on others it
    does not, and a denominator should be the best the machine demonstrably
    sustained. The kernel name travels with the number so the reader always
    knows which access pattern produced it.
    """
    if run is None:
        return None, None
    best_name, best_value = None, None
    for name, kernel in run.kernels.items():
        if name == "read_strided":
            # A different byte convention; never mixed into the ceiling.
            continue
        value = kernel.gb_s
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_name, best_value = name, value
    return best_value, best_name


def measure(options: MeasureOptions | None = None) -> Result:
    """Measure this machine's achieved memory bandwidth ceiling.

    Returns a Result whose `envelope` conforms to the
    measurement-provenance/1 envelope. Raises only on a programming error:
    an unmeasurable or untrustworthy machine produces a Result with a
    non-PASS verdict, never an exception, because the record of WHY a number
    is untrustworthy is the output that matters most.
    """
    options = options or MeasureOptions()
    started_at = _utc_now_iso()
    wall_start = time.perf_counter()
    unreadable: list = []

    # --- what are we measuring on ------------------------------------------
    topo = _topology.detect_topology(selected_cpus=options.cpus)
    unreadable.extend(topo.unreadable)

    selected = (
        tuple(sorted(set(int(c) for c in options.cpus)))
        if options.cpus
        else topo.online_cpus
    )

    peak = _theoretical.derive_theoretical_peak(options.memory_config)
    unreadable.extend(peak.unreadable)

    # --- is the machine quiet ----------------------------------------------
    busy_fraction, busy_source, busy_unreadable = _conditions.measure_competing_load(
        selected, options.load_window_s
    )
    unreadable.extend(busy_unreadable)

    # --- code path ----------------------------------------------------------
    ckernels = None
    c_unavailable_reason = None
    if options.code_path in (CODE_PATH_AUTO, _kernels.PATH_C):
        ckernels, c_unavailable_reason = load_c_kernels(
            library_path=options.c_library, build_if_missing=options.allow_c_build
        )
    if ckernels is not None:
        code_path = _kernels.PATH_C
    elif options.code_path == _kernels.PATH_C:
        code_path = _kernels.PATH_C
    else:
        code_path = _kernels.PATH_NUMPY

    if code_path == _kernels.PATH_C and ckernels is None:
        # The operator insisted on the compiled path and it is not there.
        # That is a real refusal, not a silent downgrade.
        envelope = _build_envelope(
            started_at=started_at,
            duration_s=time.perf_counter() - wall_start,
            verdict=_schema.VERDICT_UNSUPPORTED,
            reasons=[
                _validity.reason(
                    "PLATFORM_UNSUPPORTED",
                    "The compiled code path was required with --code-path c "
                    "but could not be loaded: %s" % c_unavailable_reason,
                )
            ],
            unreadable=_dedupe(unreadable),
            measurement={
                "code_path": {
                    "used": None,
                    "requested": options.code_path,
                    "c_unavailable_reason": c_unavailable_reason,
                },
                "topology": topo.as_dict(),
                "theoretical_peak": peak.as_dict(),
                "options": options.as_dict(),
            },
        )
        return Result(
            envelope=envelope,
            verdict=_schema.VERDICT_UNSUPPORTED,
            reasons=envelope["reasons"],
        )

    # --- optional, opt-in governor change ----------------------------------
    governor_change = None
    if options.set_governor:
        governor_change = _conditions.GovernorChange(options.set_governor, selected)
        governor_change.apply()

    # --- conditions before --------------------------------------------------
    before = _conditions.sample_conditions(selected)
    unreadable.extend(before.unreadable)

    # --- plan the buffers ---------------------------------------------------
    plan = _kernels.plan_buffers(
        llc_total_bytes=topo.llc_total_bytes,
        arrays=3,
        margin=options.llc_margin,
        available_bytes=topo.available_ram_bytes,
        memory_fraction=options.memory_fraction,
        requested_bytes_per_array=options.bytes_per_array,
    )

    aggregate = None
    per_cluster_runs: list = []
    run_errors: list = []

    if plan.elements_per_array > 0:
        aggregate = _kernels.measure(
            selected,
            plan,
            code_path,
            ckernels=ckernels,
            reps=options.reps,
            threads=options.threads,
            label="aggregate",
            scalar=options.scalar,
            include_strided=options.include_strided,
            min_run_seconds=options.min_run_seconds,
        )
        run_errors.extend(aggregate.errors)

        want_per_cluster = (
            topo.heterogeneous if options.per_cluster is None else options.per_cluster
        )
        if want_per_cluster and len(topo.clusters) > 1:
            for cluster in topo.clusters:
                cluster_cpus = [c for c in cluster.cpus if c in set(selected)]
                if not cluster_cpus:
                    continue
                run = _kernels.measure(
                    cluster_cpus,
                    plan,
                    code_path,
                    ckernels=ckernels,
                    reps=options.reps,
                    threads=None,
                    label=cluster.name,
                    scalar=options.scalar,
                    include_strided=options.include_strided,
                    min_run_seconds=options.min_run_seconds,
                )
                per_cluster_runs.append(run)
                run_errors.extend(run.errors)
    else:
        run_errors.append(
            "buffer plan produced zero elements per array, so nothing was "
            "measured"
        )

    # --- conditions after ---------------------------------------------------
    after = _conditions.sample_conditions(selected)
    delta = _conditions.compare_snapshots(before, after)

    if governor_change is not None:
        governor_change.restore()

    # --- interpret ----------------------------------------------------------
    ceiling_gb_s, ceiling_kernel = _ceiling(aggregate)
    fraction = None
    if ceiling_gb_s and peak.peak_gb_s:
        fraction = ceiling_gb_s / peak.peak_gb_s

    validity_input = _validity.ValidityInput(
        platform_supported=aggregate is not None,
        platform_detail=(
            ""
            if aggregate is not None
            else "No measurement was taken: " + "; ".join(run_errors)
        ),
        llc_known=plan.llc_known,
        llc_total_bytes=plan.llc_total_bytes,
        bytes_per_array=plan.bytes_per_array,
        required_bytes_per_array=plan.required_bytes_per_array,
        satisfies_margin=plan.satisfies_margin,
        insufficient_memory=plan.insufficient_memory,
        llc_margin=plan.margin,
        pinning_supported=(
            aggregate.pinning_supported if aggregate is not None else False
        ),
        affinity_as_requested=(
            aggregate.affinity_as_requested if aggregate is not None else False
        ),
        affinity_detail=_affinity_detail(aggregate),
        busy_fraction=busy_fraction,
        busy_source=busy_source,
        busy_threshold=options.busy_threshold,
        governor=before.governor,
        governor_changed=bool(delta.get("governor_changed")),
        governor_before=delta.get("governor_before"),
        governor_after=delta.get("governor_after"),
        governor_restore_verified=(
            None if governor_change is None else governor_change.restore_verified
        ),
        throttle_counters_readable=bool(before.throttle_counters),
        thermal_throttled=bool(delta["throttled_during_run"]["thermal"]),
        quota_throttled=bool(delta["throttled_during_run"]["quota"]),
        throttle_deltas=delta.get("throttle_deltas") or {},
        swap_delta=delta.get("swap_delta"),
        major_fault_delta=delta.get("major_fault_delta"),
        peak_known=peak.known,
        peak_gb_s=peak.peak_gb_s,
        achieved_gb_s=ceiling_gb_s,
        achieved_kernel=ceiling_kernel,
        code_path=code_path,
        c_unavailable_reason=c_unavailable_reason,
        timer_resolution_s=(
            aggregate.timer_resolution_s if aggregate is not None else None
        ),
        best_seconds=_best_seconds(aggregate),
        max_spread_fraction=_max_spread(aggregate),
        spread_tolerance=options.spread_tolerance,
        min_run_seconds=options.min_run_seconds,
        clusters_detected=len(topo.clusters),
        cluster_source=topo.cluster_source,
        errors=run_errors,
    )
    reasons, verdict = _validity.evaluate(validity_input)

    measurement = {
        "achieved": {
            "ceiling_gb_s": _round(ceiling_gb_s),
            "ceiling_kernel": ceiling_kernel,
            "read_gb_s": _round(_gb_s(aggregate, "read")),
            "copy_gb_s": _round(_gb_s(aggregate, "copy")),
            "triad_gb_s": _round(_gb_s(aggregate, "triad")),
            "strided_read_gb_s": _round(_gb_s(aggregate, "read_strided")),
            "fraction_of_theoretical": (
                None if fraction is None else round(fraction, 4)
            ),
            "fraction_of_theoretical_percent": (
                None if fraction is None else round(fraction * 100.0, 2)
            ),
            "definition": (
                "ceiling_gb_s is the highest of the read, copy and triad "
                "figures from the aggregate run, with ceiling_kernel naming "
                "which one. The strided read is excluded because it uses a "
                "different byte convention. GB = 10^9 bytes."
            ),
        },
        "theoretical_peak": peak.as_dict(),
        "code_path": {
            "used": code_path,
            "requested": options.code_path,
            "c_library": None if ckernels is None else ckernels.as_dict(),
            "c_unavailable_reason": c_unavailable_reason,
            "numpy_version": _numpy_version(),
            "note": (
                "Every bandwidth figure in this record was produced by the "
                "path named in code_path.used. A numpy figure and a compiled "
                "figure from the same machine are not interchangeable."
            ),
        },
        "aggregate": None if aggregate is None else aggregate.as_dict(),
        "per_cluster": [run.as_dict() for run in per_cluster_runs],
        "cluster_saturation": _cluster_saturation(aggregate, per_cluster_runs, peak),
        "topology": topo.as_dict(),
        "conditions": {
            "governor": before.governor,
            "governor_source": before.governor_source,
            "governor_per_cpu": before.governor_per_cpu,
            "governor_before": delta.get("governor_before"),
            "governor_after": delta.get("governor_after"),
            "governor_changed_during_run": bool(delta.get("governor_changed")),
            "windows_power_plan": before.power_plan,
            "pre_run_busy_fraction": (
                None if busy_fraction is None else round(busy_fraction, 4)
            ),
            "pre_run_busy_source": busy_source,
            "throttle_counter_sources": before.throttle_sources,
            "throttle_counters_before": before.throttle_counters,
            "throttle_counters_after": after.throttle_counters,
            "throttle_deltas": delta.get("throttle_deltas"),
            "swap_page_transfers_during_run": delta.get("swap_delta"),
            "major_faults_during_run": delta.get("major_fault_delta"),
            "temperature_before_c": delta.get("temperature_before_c"),
            "temperature_after_c": delta.get("temperature_after_c"),
            "governor_change_requested": (
                None if governor_change is None else governor_change.as_dict()
            ),
        },
        "thresholds": {
            "llc_margin": plan.margin,
            "busy_threshold": options.busy_threshold,
            "spread_tolerance": options.spread_tolerance,
            "min_run_seconds": options.min_run_seconds,
            "timer_resolution_ratio": _validity.DEFAULT_TIMER_RESOLUTION_RATIO,
        },
        "options": options.as_dict(),
        "run_errors": run_errors,
    }

    envelope = _build_envelope(
        started_at=started_at,
        duration_s=time.perf_counter() - wall_start,
        verdict=verdict,
        reasons=reasons,
        unreadable=_dedupe(unreadable),
        measurement=measurement,
    )

    return Result(
        envelope=envelope,
        verdict=verdict,
        reasons=reasons,
        ceiling_gb_s=ceiling_gb_s,
        ceiling_kernel=ceiling_kernel,
        read_gb_s=_gb_s(aggregate, "read"),
        copy_gb_s=_gb_s(aggregate, "copy"),
        triad_gb_s=_gb_s(aggregate, "triad"),
        theoretical_peak_gb_s=peak.peak_gb_s,
        fraction_of_theoretical=fraction,
        code_path=code_path,
    )


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


def _numpy_version():
    try:
        import numpy

        return numpy.__version__
    except Exception:
        return None


def _affinity_detail(run) -> str:
    if run is None:
        return ""
    problems = []
    for result in run.affinity:
        if result.as_requested:
            continue
        problems.append(
            "requested %s, effective %s%s"
            % (
                _topology.format_cpu_list(result.requested),
                (
                    "unreadable"
                    if result.effective is None
                    else _topology.format_cpu_list(result.effective)
                ),
                "" if not result.error else " (%s)" % result.error,
            )
        )
    return "; ".join(problems)


def _cluster_saturation(aggregate, per_cluster_runs, peak) -> dict | None:
    """Compare the aggregate ceiling with the best single cluster's.

    This is the per-cluster rationale made machine readable. On a
    heterogeneous SoC whose clusters share one memory controller, the
    aggregate can sit very close to the best single cluster, in which case a
    single aggregate number tells a reader nothing about where the bandwidth
    came from. membw reports the comparison and declines to interpret it
    beyond stating the arithmetic.
    """
    if aggregate is None or not per_cluster_runs:
        return None
    aggregate_value, aggregate_kernel = _ceiling(aggregate)
    rows = []
    for run in per_cluster_runs:
        value, kernel = _ceiling(run)
        rows.append(
            {
                "cluster": run.label,
                "cpu_list": _topology.format_cpu_list(run.cpus),
                "ceiling_gb_s": _round(value),
                "ceiling_kernel": kernel,
                "fraction_of_theoretical_percent": (
                    None
                    if not (value and peak.peak_gb_s)
                    else round(value / peak.peak_gb_s * 100.0, 2)
                ),
            }
        )
    usable = [r for r in rows if r["ceiling_gb_s"]]
    best = max(usable, key=lambda r: r["ceiling_gb_s"]) if usable else None
    gain = None
    if best and aggregate_value:
        gain = (aggregate_value - best["ceiling_gb_s"]) / best["ceiling_gb_s"]
    return {
        "aggregate_ceiling_gb_s": _round(aggregate_value),
        "aggregate_ceiling_kernel": aggregate_kernel,
        "per_cluster": rows,
        "best_single_cluster": None if best is None else best["cluster"],
        "best_single_cluster_ceiling_gb_s": None if best is None else best["ceiling_gb_s"],
        "aggregate_gain_over_best_cluster_fraction": (
            None if gain is None else round(gain, 4)
        ),
        "aggregate_gain_over_best_cluster_percent": (
            None if gain is None else round(gain * 100.0, 2)
        ),
        "note": (
            "A small gain from adding the remaining clusters means the "
            "clusters share a memory controller that one of them already "
            "nearly saturates. membw reports the arithmetic and leaves the "
            "conclusion to the reader."
        ),
    }


def _build_envelope(
    started_at: str,
    duration_s: float,
    verdict: str,
    reasons: list,
    unreadable: list,
    measurement: dict,
) -> dict:
    """Assemble the measurement-provenance/1 envelope.

    The key order here is the envelope's documented order, so two tools'
    output diffs cleanly.
    """
    return {
        "schema": _schema.SCHEMA_ID,
        "tool": {"name": _schema.TOOL_NAME, "version": __version__},
        "taken_at_utc": started_at,
        "duration_s": round(duration_s, 4),
        "platform": _platform_block(),
        "verdict": verdict,
        "reasons": reasons,
        "unreadable": list(unreadable),
        "measurement": measurement,
    }
