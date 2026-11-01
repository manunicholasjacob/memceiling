"""The measurement itself: buffer sizing, both code paths, pinned threads.

Two code paths produce the numbers:

  * "c"     a compiled shared library, one foreign call per timed region,
            with ctypes releasing the GIL for its duration.
  * "numpy" numpy ufuncs. Portable and always available, but its triad
            cannot be expressed as one fused pass, so it does more array
            passes than the byte count it is credited with. membw records
            the path on every number rather than treating the two as
            interchangeable.

Byte conventions are STREAM's, stated on every figure:

  read   n * 8                 one array read
  copy   2 * n * 8             one array read plus one array written
  triad  3 * n * 8             two arrays read plus one array written

That convention counts the bytes the program asked for, not the bytes the
DRAM moved. On a machine with a write-allocate cache the write in Copy also
fetches the line it is about to overwrite, so true DRAM traffic for Copy can
be three arrays, not two. Every tool in this family shares that convention,
so the numbers stay comparable; the README says so under Limitations.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .affinity import current_cpu, pin_current_thread
from .topology import format_cpu_list

PATH_C = "c"
PATH_NUMPY = "numpy"

CACHE_LINE_BYTES = 64
ELEMENT_BYTES = 8  # float64, as STREAM uses by default
MIB = 1024 * 1024

# STREAM's run rules put a floor of one million elements per array under the
# cache-multiple rule, so membw uses the same floor.
STREAM_MIN_ELEMENTS = 1_000_000

# Default working-set margin. STREAM's run rules require each array to be at
# least 4x the sum of all the last-level caches used in the run; membw
# enforces that same multiple so a membw buffer is never smaller than a
# conforming STREAM buffer.
DEFAULT_LLC_MARGIN = 4.0

# Floor used when the LLC size cannot be read at all. 64 MiB clears the
# last-level cache of every CPU membw is likely to meet, but it is a floor,
# not a check, and the run is reported as UNKNOWN rather than PASS when the
# real LLC size was never read.
DEFAULT_FLOOR_BYTES = 64 * MIB

# Share of available memory membw is willing to occupy with its buffers.
DEFAULT_MEMORY_FRACTION = 0.5

# Ceiling on the auto-calibrated pass count, so a pathologically fast
# calibration cannot turn one kernel into a multi-minute run.
MAX_INNER_PASSES = 256

# The calibration pass can be slower than the timed ones, so the pass count
# aims slightly above the minimum run length rather than exactly at it.
INNER_SAFETY_FACTOR = 1.25


# ---------------------------------------------------------------------------
# Buffer sizing (pure, so the refusal logic is testable without a machine)
# ---------------------------------------------------------------------------


@dataclass
class BufferPlan:
    """How large the arrays will be, and whether that clears the LLC."""

    elements_per_array: int
    bytes_per_array: int
    arrays: int
    margin: float
    llc_total_bytes: int | None
    required_bytes_per_array: int | None
    satisfies_margin: bool
    llc_known: bool
    memory_limited: bool
    insufficient_memory: bool
    rationale: str

    @property
    def total_bytes(self) -> int:
        return self.bytes_per_array * self.arrays

    def as_dict(self) -> dict:
        return {
            "elements_per_array": self.elements_per_array,
            "bytes_per_array": self.bytes_per_array,
            "arrays": self.arrays,
            "total_bytes": self.total_bytes,
            "llc_margin": self.margin,
            "llc_total_bytes": self.llc_total_bytes,
            "required_bytes_per_array": self.required_bytes_per_array,
            "satisfies_margin": self.satisfies_margin,
            "llc_size_known": self.llc_known,
            "memory_limited": self.memory_limited,
            "insufficient_memory": self.insufficient_memory,
            "element_bytes": ELEMENT_BYTES,
            "rationale": self.rationale,
        }


def plan_buffers(
    llc_total_bytes: int | None,
    arrays: int = 3,
    margin: float = DEFAULT_LLC_MARGIN,
    available_bytes: int | None = None,
    floor_bytes: int = DEFAULT_FLOOR_BYTES,
    memory_fraction: float = DEFAULT_MEMORY_FRACTION,
    requested_bytes_per_array: int | None = None,
) -> BufferPlan:
    """Decide the array size, and say whether it clears the last-level cache.

    The rule: each array must be at least `margin` times the summed size of
    the last-level caches reachable from the CPUs under test, and never
    smaller than STREAM's one-million-element floor.

    If available memory will not allow that, the plan comes back with
    satisfies_margin False and (when it is badly short) insufficient_memory
    True. It is the caller's job to turn that into a refusal; this function
    only does the arithmetic, which is why it takes plain integers and is
    tested without touching a machine.
    """
    llc_known = bool(llc_total_bytes)
    if llc_known:
        required = int(math.ceil(margin * llc_total_bytes))
        target = max(required, STREAM_MIN_ELEMENTS * ELEMENT_BYTES)
        rationale = (
            "each array sized to at least %.1fx the summed last-level cache "
            "(%d bytes), with STREAM's one-million-element floor"
            % (margin, llc_total_bytes)
        )
    else:
        required = None
        target = max(floor_bytes, STREAM_MIN_ELEMENTS * ELEMENT_BYTES)
        rationale = (
            "last-level cache size was not readable, so each array was sized "
            "to the %d byte fallback floor; the margin was NOT verified"
            % floor_bytes
        )

    if requested_bytes_per_array:
        target = int(requested_bytes_per_array)
        rationale = (
            "array size was set explicitly to %d bytes by the operator"
            % target
        )

    memory_limited = False
    if available_bytes:
        cap = int(available_bytes * memory_fraction) // max(arrays, 1)
        if cap < target:
            target = cap
            memory_limited = True
            rationale += (
                "; capped at %d bytes per array because only %d bytes were "
                "available and membw will not occupy more than %.0f%% of it"
                % (cap, available_bytes, memory_fraction * 100)
            )

    # Whole number of cache lines, and a whole number of elements.
    elements = max(0, int(target) // (ELEMENT_BYTES * 8) * 8)
    bytes_per_array = elements * ELEMENT_BYTES

    satisfies = bool(llc_known and required is not None and bytes_per_array >= required)
    insufficient = elements < STREAM_MIN_ELEMENTS

    return BufferPlan(
        elements_per_array=elements,
        bytes_per_array=bytes_per_array,
        arrays=arrays,
        margin=margin,
        llc_total_bytes=llc_total_bytes,
        required_bytes_per_array=required,
        satisfies_margin=satisfies,
        llc_known=llc_known,
        memory_limited=memory_limited,
        insufficient_memory=insufficient,
        rationale=rationale,
    )


def split_evenly(total: int, parts: int) -> list:
    """Split `total` elements into `parts` contiguous (start, length) slices.

    Slice boundaries land on cache-line multiples wherever the arithmetic
    allows, so two threads never share a cache line and the measurement is
    not polluted by false sharing on the boundary.
    """
    if parts <= 0:
        return []
    if parts == 1:
        return [(0, total)]
    per_line = CACHE_LINE_BYTES // ELEMENT_BYTES
    chunk = (total // parts) // per_line * per_line
    if chunk == 0:
        chunk = total // parts
    slices = []
    start = 0
    for index in range(parts):
        length = chunk if index < parts - 1 else total - start
        if length <= 0:
            length = 0
        slices.append((start, length))
        start += length
    return slices


def assign_cpus(cpus, threads: int | None) -> list:
    """Give each worker thread exactly one CPU to be pinned to.

    With threads unset there is one thread per CPU, which is the
    configuration a bandwidth ceiling wants. With threads set higher than
    the CPU count, CPUs are reused round-robin, and the record shows the
    oversubscription.
    """
    ordered = sorted(set(int(c) for c in cpus))
    if not ordered:
        return []
    count = threads or len(ordered)
    return [[ordered[i % len(ordered)]] for i in range(count)]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def choose_inner_passes(
    calibration_s: float | None,
    min_run_seconds: float,
    max_inner: int = MAX_INNER_PASSES,
) -> int:
    """How many passes over the buffers one timed region should contain.

    A single pass over a buffer sized to a few times the last-level cache
    takes only a few milliseconds on a modern machine, which is short enough
    that the clock and the scheduler contribute visible noise. membw
    therefore times N passes and divides, choosing N from one calibration
    pass so that the timed region reaches min_run_seconds.

    Re-reading a buffer that is already several times larger than the
    last-level cache does not turn the measurement into a cache
    measurement: by the time the second pass starts, the beginning of the
    buffer has long been evicted. That is the same reasoning under mbw's
    and STREAM's repeat loops.

    The calibration pass is itself a measurement and may be slower than the
    timed ones, which would leave the timed region under the floor, so the
    target is over-provisioned by INNER_SAFETY_FACTOR.
    """
    if not calibration_s or calibration_s <= 0:
        return 1
    needed = int(math.ceil(min_run_seconds * INNER_SAFETY_FACTOR / calibration_s))
    return max(1, min(max_inner, needed))


@dataclass
class KernelMeasurement:
    """One kernel's timings and the bandwidth they imply."""

    name: str
    bytes_per_rep: int
    byte_convention: str
    times_s: list
    code_path: str
    threads: int
    note: str | None = None
    inner_passes: int = 1
    calibration_s: float | None = None

    @property
    def best_s(self) -> float | None:
        valid = [t for t in self.times_s if t > 0]
        return min(valid) if valid else None

    @property
    def worst_s(self) -> float | None:
        valid = [t for t in self.times_s if t > 0]
        return max(valid) if valid else None

    @property
    def gb_s(self) -> float | None:
        best = self.best_s
        if not best:
            return None
        return self.bytes_per_rep / best / 1e9

    @property
    def gb_s_worst(self) -> float | None:
        worst = self.worst_s
        if not worst:
            return None
        return self.bytes_per_rep / worst / 1e9

    @property
    def spread_fraction(self) -> float | None:
        """(worst - best) / best over the repeats, or None."""
        best, worst = self.best_s, self.worst_s
        if not best or not worst:
            return None
        return (worst - best) / best

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "gb_s": None if self.gb_s is None else round(self.gb_s, 4),
            "gb_s_slowest_repeat": (
                None if self.gb_s_worst is None else round(self.gb_s_worst, 4)
            ),
            "bytes_per_repeat": self.bytes_per_rep,
            "byte_convention": self.byte_convention,
            "best_seconds": None if self.best_s is None else round(self.best_s, 9),
            "repeat_seconds": [round(t, 9) for t in self.times_s],
            "repeat_spread_fraction": (
                None
                if self.spread_fraction is None
                else round(self.spread_fraction, 6)
            ),
            "threads": self.threads,
            "code_path": self.code_path,
            "inner_passes_per_repeat": self.inner_passes,
            "calibration_seconds": (
                None if self.calibration_s is None else round(self.calibration_s, 9)
            ),
            "note": self.note,
        }


@dataclass
class RunMeasurement:
    """Everything one pinned measurement produced."""

    label: str
    cpus: tuple
    threads: int
    code_path: str
    kernels: dict = field(default_factory=dict)
    affinity: list = field(default_factory=list)
    observed_cpus: tuple = ()
    plan: BufferPlan | None = None
    timer_resolution_s: float | None = None
    errors: list = field(default_factory=list)

    @property
    def affinity_as_requested(self) -> bool:
        return bool(self.affinity) and all(a.as_requested for a in self.affinity)

    @property
    def pinning_supported(self) -> bool:
        return bool(self.affinity) and all(a.supported for a in self.affinity)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "cpu_list": format_cpu_list(self.cpus),
            "cpu_count": len(self.cpus),
            "threads": self.threads,
            "code_path": self.code_path,
            "kernels": {name: k.as_dict() for name, k in self.kernels.items()},
            "affinity": {
                "requested_cpu_list": format_cpu_list(self.cpus),
                "every_thread_as_requested": self.affinity_as_requested,
                "pinning_supported": self.pinning_supported,
                "observed_cpu_list": format_cpu_list(self.observed_cpus),
                "per_thread": [a.as_dict() for a in self.affinity],
            },
            "buffers": None if self.plan is None else self.plan.as_dict(),
            "timer_resolution_s": self.timer_resolution_s,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def allocate_aligned(elements: int, alignment: int = CACHE_LINE_BYTES):
    """Allocate a float64 array whose first element is `alignment`-aligned.

    numpy's allocator usually returns aligned memory for large requests but
    does not promise it, and an unaligned array quietly costs bandwidth, so
    membw aligns explicitly. The returned view keeps the oversized base
    array alive, so nothing has to be held on the caller's side.
    """
    raw = np.empty(elements * ELEMENT_BYTES + alignment, dtype=np.uint8)
    offset = (-raw.ctypes.data) % alignment
    return raw[offset : offset + elements * ELEMENT_BYTES].view(np.float64)


def _ptr(array, offset_elements: int):
    address = array.ctypes.data + offset_elements * ELEMENT_BYTES
    return ctypes.cast(address, ctypes.POINTER(ctypes.c_double))


# ---------------------------------------------------------------------------
# The pinned thread harness
# ---------------------------------------------------------------------------


def _run_pinned(cpu_assignment, reps: int, work, first_touch=None):
    """Run `work(index, rep)` on one pinned thread per entry in cpu_assignment.

    Each thread pins itself FIRST, then does its first touch (so pages land
    on the right node or cluster), then synchronises on a barrier before
    every timed repetition. The elapsed time for a repetition is the span
    from the earliest thread start to the latest thread end, which is the
    aggregation STREAM uses and the only one that does not credit a slow
    thread with a fast thread's time.

    Returns (per_rep_seconds, affinity_results, observed_cpus, errors).
    """
    count = len(cpu_assignment)
    barrier = threading.Barrier(count)
    affinity_results = [None] * count
    starts = [[0.0] * count for _ in range(reps)]
    ends = [[0.0] * count for _ in range(reps)]
    observed = [set() for _ in range(count)]
    errors: list = []
    errors_lock = threading.Lock()

    def worker(index: int):
        try:
            affinity_results[index] = pin_current_thread(cpu_assignment[index])
            cpu = current_cpu()
            if cpu is not None:
                observed[index].add(cpu)
            if first_touch is not None:
                first_touch(index)
            for rep in range(reps):
                barrier.wait()
                t0 = time.perf_counter()
                work(index, rep)
                t1 = time.perf_counter()
                starts[rep][index] = t0
                ends[rep][index] = t1
                cpu = current_cpu()
                if cpu is not None:
                    observed[index].add(cpu)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            with errors_lock:
                errors.append("thread %d: %s: %s" % (index, type(exc).__name__, exc))
            barrier.abort()

    threads = [
        threading.Thread(target=worker, args=(i,), name="membw-%d" % i, daemon=True)
        for i in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    per_rep = []
    for rep in range(reps):
        rep_starts = [s for s in starts[rep] if s > 0]
        rep_ends = [e for e in ends[rep] if e > 0]
        if len(rep_starts) != count or len(rep_ends) != count:
            continue
        per_rep.append(max(rep_ends) - min(rep_starts))

    all_observed = tuple(sorted({c for s in observed for c in s}))
    return per_rep, [a for a in affinity_results if a is not None], all_observed, errors


# ---------------------------------------------------------------------------
# Kernel drivers
# ---------------------------------------------------------------------------


def measure(
    cpus,
    plan: BufferPlan,
    code_path: str,
    ckernels=None,
    reps: int = 5,
    threads: int | None = None,
    label: str = "aggregate",
    scalar: float = 3.0,
    include_strided: bool = False,
    min_run_seconds: float = 0.05,
) -> RunMeasurement:
    """Measure read, copy and triad on `cpus` using one code path.

    `code_path` is PATH_C or PATH_NUMPY. PATH_C requires `ckernels`, a loaded
    CKernels from membw.cbuild. Buffers are allocated here and released when
    this function returns, so a per-cluster sweep does not hold three
    buffers per cluster at once.
    """
    if code_path == PATH_C and ckernels is None:
        raise ValueError("code_path 'c' needs a loaded kernel library")

    assignment = assign_cpus(cpus, threads)
    if not assignment:
        return RunMeasurement(
            label=label,
            cpus=tuple(sorted(set(int(c) for c in cpus))),
            threads=0,
            code_path=code_path,
            errors=["no CPUs to measure on"],
            plan=plan,
        )

    thread_count = len(assignment)
    n = plan.elements_per_array
    slices = split_evenly(n, thread_count)

    a = allocate_aligned(n)
    b = allocate_aligned(n)
    c = allocate_aligned(n)

    lib = ckernels.lib if ckernels is not None else None

    def first_touch(index: int):
        start, length = slices[index]
        if length <= 0:
            return
        if code_path == PATH_C:
            lib.membw_fill(_ptr(a, start), 1.0, length)
            lib.membw_fill(_ptr(b, start), 2.0, length)
            lib.membw_fill(_ptr(c, start), 0.5, length)
        else:
            a[start : start + length] = 1.0
            b[start : start + length] = 2.0
            c[start : start + length] = 0.5

    kernels: dict = {}
    timer_resolution = time.get_clock_info("perf_counter").resolution
    affinity: list = []
    observed: tuple = ()
    errors: list = []

    # Read by every work function; set by record() during calibration.
    inner_box = [1]

    def record(name, work, bytes_per_pass, convention, note=None):
        nonlocal affinity, observed, errors

        # Calibration: two passes with inner = 1, the first of which also
        # does the first touch. The second is used for the timing, so the
        # calibration figure is not distorted by page faults.
        inner_box[0] = 1
        cal_times, affinity_results, observed_cpus, cal_errors = _run_pinned(
            assignment, 2, work, first_touch=first_touch
        )
        if affinity_results:
            affinity = affinity_results
        observed = tuple(sorted(set(observed) | set(observed_cpus)))
        errors.extend(cal_errors)
        calibration_s = cal_times[-1] if cal_times else None
        inner = choose_inner_passes(calibration_s, min_run_seconds)
        inner_box[0] = inner

        times, affinity_results, observed_cpus, work_errors = _run_pinned(
            assignment, reps, work
        )
        if affinity_results:
            affinity = affinity_results
        observed = tuple(sorted(set(observed) | set(observed_cpus)))
        errors.extend(work_errors)

        kernels[name] = KernelMeasurement(
            name=name,
            bytes_per_rep=bytes_per_pass * inner,
            byte_convention=convention,
            times_s=times,
            code_path=code_path,
            threads=thread_count,
            note=note,
            inner_passes=inner,
            calibration_s=calibration_s,
        )

    sink = [0.0] * thread_count

    if code_path == PATH_C:

        def read_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            pointer = _ptr(a, start)
            for _ in range(inner_box[0]):
                sink[index] = lib.membw_read(pointer, length)

        def copy_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            dst, src = _ptr(c, start), _ptr(a, start)
            for _ in range(inner_box[0]):
                lib.membw_copy(dst, src, length)

        def triad_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            pa, pb, pc = _ptr(a, start), _ptr(b, start), _ptr(c, start)
            for _ in range(inner_box[0]):
                lib.membw_triad(pa, pb, pc, scalar, length)

        def strided_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            pointer = _ptr(a, start)
            step = CACHE_LINE_BYTES // ELEMENT_BYTES
            for _ in range(inner_box[0]):
                sink[index] = lib.membw_read_strided(pointer, length, step)

        read_note = (
            "one compiled loop per pass, four accumulators, result returned "
            "so the loads cannot be optimised away"
        )
        triad_note = "one fused compiled pass over three arrays"
    else:

        def read_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            view = a[start : start + length]
            for _ in range(inner_box[0]):
                sink[index] = float(np.add.reduce(view))

        def copy_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            dst = c[start : start + length]
            src = a[start : start + length]
            for _ in range(inner_box[0]):
                np.copyto(dst, src)

        def triad_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            av = a[start : start + length]
            bv = b[start : start + length]
            cv = c[start : start + length]
            for _ in range(inner_box[0]):
                # numpy has no fused a = b + q*c, so this is two ufunc
                # passes using `a` itself as the temporary. It therefore
                # moves five arrays' worth of traffic while being credited
                # with STREAM's three, which makes this number a LOWER
                # BOUND on what a fused kernel achieves.
                np.multiply(cv, scalar, out=av)
                np.add(av, bv, out=av)

        def strided_work(index, rep):
            start, length = slices[index]
            if not length:
                return
            step = CACHE_LINE_BYTES // ELEMENT_BYTES
            view = a[start : start + length : step]
            for _ in range(inner_box[0]):
                sink[index] = float(np.add.reduce(view))

        read_note = (
            "np.add.reduce over a float64 view; the reduction is "
            "bandwidth-bound but carries numpy per-call overhead"
        )
        triad_note = (
            "two numpy ufunc passes, not a fused triad: credited with "
            "STREAM's three arrays while moving five, so this is a lower "
            "bound on a fused C triad"
        )

    record(
        "read",
        read_work,
        bytes_per_pass=n * ELEMENT_BYTES,
        convention="one array read (n * %d bytes)" % ELEMENT_BYTES,
        note=read_note,
    )
    record(
        "copy",
        copy_work,
        bytes_per_pass=2 * n * ELEMENT_BYTES,
        convention="STREAM Copy: one array read plus one written (2 * n * %d bytes)"
        % ELEMENT_BYTES,
    )
    record(
        "triad",
        triad_work,
        bytes_per_pass=3 * n * ELEMENT_BYTES,
        convention="STREAM Triad: two arrays read plus one written (3 * n * %d bytes)"
        % ELEMENT_BYTES,
        note=triad_note,
    )
    if include_strided:
        step = CACHE_LINE_BYTES // ELEMENT_BYTES
        touched = (n + step - 1) // step
        record(
            "read_strided",
            strided_work,
            bytes_per_pass=touched * CACHE_LINE_BYTES,
            convention=(
                "cache lines fetched: one %d byte line per touched element "
                "(%d lines), NOT comparable with the sequential read figure"
                % (CACHE_LINE_BYTES, touched)
            ),
        )

    del a, b, c

    return RunMeasurement(
        label=label,
        cpus=tuple(sorted(set(int(x) for x in cpus))),
        threads=thread_count,
        code_path=code_path,
        kernels=kernels,
        affinity=affinity,
        observed_cpus=observed,
        plan=plan,
        timer_resolution_s=timer_resolution,
        errors=errors,
    )
