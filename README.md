# membw

Measure a machine's **achieved** memory bandwidth ceiling, report it against
the theoretical peak derived from the memory configuration that could
actually be read, and attach a record of the conditions the figure was taken
under. When those conditions invalidate the figure, say so instead of
printing a number.

`membw` is a library, a CLI and a GitHub Action.

## Why this exists

Anyone doing a roofline or a memory-bound analysis needs a denominator, and
today there are two usual ways to get one. The first is to quote the
vendor's theoretical peak, which is wrong, often by 25 to 30 percent. The
second is to run STREAM by hand and report the figure with no record of the
conditions, which makes it unreproducible and uncomparable.

Neither habit survives contact with a heterogeneous SoC, where a single
aggregate number can be close to meaningless, or with a shared CI runner,
where the machine may have been thermally throttled or quota throttled for
part of the run.

### The author's prior measurements, which are why the tool has this shape

These are first-party results measured by the author before `membw` existed.
They are stated here as prior findings, not as output of this tool. `membw`
did not produce them and does not reproduce them.

- **Intel i7-12700H (DDR5).** STREAM Triad 53.67 GB/s, Copy 56.70 GB/s,
  reconciled read ceiling 53.9 GB/s, which is about 70 percent of
  theoretical. An earlier cross-paper figure of 42 GB/s was wrong and was
  corrected against a first-party receipt. That correction is the reason
  `membw` records a provenance record at all: a number without one cannot be
  audited later.
- **Raspberry Pi 5, Cortex-A76 (LPDDR4X).** 12.44 GB/s strided C read under
  the performance governor, which is 73 percent of the 17.07 GB/s
  theoretical. STREAM Triad 5.25 GB/s and Copy 5.82 GB/s, so the write path
  roughly halves against the read path, unlike DDR5 on x86. A numpy-based
  tool reported 13.79 GB/s for the same machine. That discrepancy is the
  reason `membw` labels every figure with the code path that produced it.
- **Samsung Galaxy M21 (Arm big.LITTLE, normal power mode).** The big A73
  cluster alone reaches 7.51 GB/s and nearly saturates the shared
  controller. Adding four A53 cores takes it only to 7.84 GB/s, a 4 percent
  gain. A53 only is 4.57 GB/s. Per-cluster saturation, not per-core scaling.

That last result is the design requirement most tools miss. On a
heterogeneous Arm SoC a single aggregate number does not say where the
bandwidth came from, so `membw` measures and reports **per cluster** as well
as in aggregate, and records which cores it was pinned to.

## What one run reports

- achieved sequential read bandwidth
- STREAM-convention Copy and Triad
- the theoretical peak derived from the memory configuration, or `UNKNOWN`
- the achieved fraction of theoretical
- the thread count and the core cluster configuration used
- the CPU affinity mask **actually in effect**, not the one requested
- the CPU frequency governor, and whether it changed across the run
- the working set size and whether it clears the last-level cache
- which code path produced the number, compiled C or numpy
- every source that could not be read
- a verdict: `PASS`, `INVALID`, `UNKNOWN` or `UNSUPPORTED`

## Install

```sh
pip install membw
```

The wheel is pure Python and depends only on numpy. The compiled kernel path
is optional and is built on the target machine from C source that ships
inside the wheel:

```sh
membw build-c
```

If no C compiler is present, that command says so and `membw` uses the numpy
path, labelling every figure accordingly. Nothing fails.

From a checkout:

```sh
pip install -e ".[dev]"
python -m pytest
```

## Usage

### CLI

```sh
membw                       # measure, human-readable summary
membw --json                # measure, print the provenance envelope
membw --json --out bw.json  # and write it to a file
membw info                  # report what can be read, measure nothing
membw build-c               # build the optional compiled kernels
membw schema                # print the reason-code vocabulary
```

Useful flags:

```sh
membw --cpus 0-11                 # measure only these CPUs
membw --per-cluster               # force per-cluster measurement
membw --code-path c               # require the compiled path, do not fall back
membw --code-path numpy           # force the portable path
membw --repeats 10                # more timed repetitions, best is reported
membw --min-run-seconds 0.5       # longer timed regions
membw --llc-margin 8              # a stricter working-set margin than STREAM's 4
membw --memory-config 4267:32:1   # supply the memory configuration yourself
membw --include-strided           # also measure a cache-line strided read
membw --exit-zero                 # record the verdict without failing the shell
```

Exit status is the verdict, so a script can act on it:

| status | meaning |
| --- | --- |
| 0 | `PASS`, the figure is trustworthy |
| 2 | `INVALID`, conditions during the run invalidate the figure |
| 3 | `UNKNOWN`, `membw` could not establish validity |
| 4 | `UNSUPPORTED`, `membw` cannot measure here |
| 1 | an internal error |

### Library

The API is small on purpose, so a dependent can rely on it:

```python
from membw import measure, MeasureOptions

result = measure()

if not result.trustworthy:            # True only when the verdict is PASS
    for item in result.reasons:
        print(item["code"], item["detail"])
    raise SystemExit("refusing to plot a roofline on an untrusted ceiling")

denominator = result.ceiling_gb_s     # GB/s, GB = 10^9 bytes
print(result.ceiling_kernel)          # which kernel produced it
print(result.fraction_of_theoretical) # or None when the peak is UNKNOWN
print(result.code_path)               # "c" or "numpy"
print(result.to_json())               # the full envelope
```

With options:

```python
result = measure(MeasureOptions(cpus=[0, 1, 2, 3], per_cluster=True, reps=10))
```

The other public entry points, for callers who want one piece rather than a
whole run:

```python
from membw import (
    detect_topology,          # CPUs, clusters, last-level cache, RAM
    derive_theoretical_peak,  # the denominator, or UNKNOWN
    plan_buffers,             # array sizing and the margin check
    build_reasons,            # the refusal rules, as pure logic
    verdict_from_reasons,
    load_c_kernels,
    build_c_kernels,
    pin_current_thread,       # returns the mask actually in effect
    SCHEMA_ID, REASON_CODES, VERDICTS,
)
```

### GitHub Action

`action.yml` is at the repository root, so the Action can be referenced
directly. Its job is to record the runner's real memory ceiling into the
build, so performance numbers from CI are interpretable.

```yaml
- uses: actions/setup-python@v5
  with:
    python-version: "3.13"

- id: membw
  uses: manunicholasjacob/membw@v0.1.0
  with:
    repeats: "3"
    min-run-seconds: "0.2"
    output-path: membw-envelope.json
    fail-on-invalid: "false"

- run: |
    echo "ceiling ${{ steps.membw.outputs.ceiling-gb-s }} GB/s"
    echo "verdict ${{ steps.membw.outputs.verdict }}"
```

Outputs: `verdict`, `ceiling-gb-s`, `ceiling-kernel`, `read-gb-s`,
`copy-gb-s`, `triad-gb-s`, `theoretical-peak-gb-s`, `fraction-percent`,
`code-path`, `envelope-path`. The Action also writes a table and the full
reason list to the job summary.

`fail-on-invalid` defaults to `false`. A shared CI runner is noisy and often
hides its SMBIOS, so a non-`PASS` verdict there is expected rather than a
build break. The verdict travels with the number either way, which is the
whole point.

## Output schema

Every run emits one JSON object. The outer envelope is
`measurement-provenance/1`, shared with the sibling tools named under
[Related tools](#related-tools), so a consumer can read the verdict and the
provenance of any of them without knowing which produced the file.

```json
{
  "schema": "measurement-provenance/1",
  "tool": {"name": "membw", "version": "0.1.0"},
  "taken_at_utc": "2026-11-01T17:33:02Z",
  "duration_s": 42.1,
  "platform": {"system": "Linux", "release": "6.8.0", "machine": "x86_64",
               "python": "3.13.1"},
  "verdict": "PASS",
  "reasons": [
    {"code": "GOVERNOR_NOT_PERFORMANCE", "severity": "warning",
     "detail": "One sentence saying what was observed."}
  ],
  "unreadable": ["dmidecode -t memory (permission denied or unreadable)"],
  "measurement": { }
}
```

`verdict` is one of `PASS`, `INVALID`, `UNKNOWN`, `UNSUPPORTED`. `reasons`
entries always have exactly `code`, `severity` and `detail`, with severity
one of `invalidating`, `warning`, `info`. `unreadable` names every source
that could not be read, so a consumer can tell a clean machine from an
unexamined one.

Everything specific to `membw` is under `measurement`:

| key | what it holds |
| --- | --- |
| `achieved` | read, copy, triad, strided read, the ceiling and the kernel that produced it, the fraction of theoretical |
| `theoretical_peak` | the derived peak, its status, the formula, the formula's inputs, the source, the modules read |
| `code_path` | `used` (`c` or `numpy`), the compiled library path, compiler and flags, why the compiled path was not used |
| `aggregate` | the whole-machine run: per-kernel timings, byte conventions, pass counts, affinity per thread, buffer plan, clock resolution |
| `per_cluster` | the same block, once per CPU cluster |
| `cluster_saturation` | the aggregate ceiling against the best single cluster's, and the gain from adding the rest |
| `topology` | CPU model, online CPUs, clusters and how they were detected, last-level cache, RAM |
| `conditions` | governor before and after, pre-run busy fraction and its source, throttle counters before, after and delta, swap and major faults during the run, temperatures |
| `thresholds` | the threshold values the verdict was reached under |
| `options` | the options the run used, including any operator overrides |

### Verdict rule

In order:

1. `PLATFORM_UNSUPPORTED` present, so the verdict is `UNSUPPORTED`.
2. Any reason of severity `invalidating`, so the verdict is `INVALID`.
3. Any confidence-blocking warning, so the verdict is `UNKNOWN`.
4. Otherwise `PASS`.

Step 3 is the part that matters. A warning saying "the cache size could not
be read" is not a clean run, it is an unverified one, and it must not come
out as `PASS`. The confidence-blocking codes are `LLC_SIZE_UNKNOWN`,
`LOAD_UNREADABLE`, `THROTTLE_COUNTERS_UNREADABLE`, `AFFINITY_UNSUPPORTED`
and `GOVERNOR_UNKNOWN`.

### Reason codes

`membw` owns these codes. `membw schema --json` prints them at runtime.

**Invalidating**, so the verdict becomes `INVALID`:

| code | meaning |
| --- | --- |
| `WORKING_SET_BELOW_LLC` | Per-array working set did not exceed the last-level cache by the required margin, so the figure may be a cache bandwidth. |
| `MEMORY_INSUFFICIENT` | Available memory was too small to allocate buffers that clear the last-level cache by the required margin. |
| `COMPETING_LOAD` | Another workload was using the target CPUs before the run. |
| `THERMAL_THROTTLE_DURING_RUN` | A thermal throttle counter advanced during the run. |
| `QUOTA_THROTTLE_DURING_RUN` | A cgroup CPU quota throttle counter advanced during the run. |
| `SWAP_ACTIVE_DURING_RUN` | Pages were swapped in or out during the run. |
| `GOVERNOR_CHANGED_DURING_RUN` | The governor was not the same before and after the run. |
| `GOVERNOR_RESTORE_FAILED` | `membw` changed the governor on request and could not verify the restore. |
| `AFFINITY_NOT_AS_REQUESTED` | The mask actually in effect differed from the one requested. |
| `FRACTION_ABOVE_THEORETICAL` | Achieved bandwidth exceeded the derived theoretical peak, so one of the two is wrong. |
| `PLATFORM_UNSUPPORTED` | `membw` cannot measure on this platform. Produces `UNSUPPORTED`. |

**Warnings**. The five marked as blocking produce `UNKNOWN` when nothing
invalidating is present; the rest are recorded and still allow `PASS`:

| code | blocking | meaning |
| --- | --- | --- |
| `LLC_SIZE_UNKNOWN` | yes | Cache size unreadable, so the working-set margin was not checked. |
| `LOAD_UNREADABLE` | yes | Busy fraction unreadable, so competing load was not ruled out. |
| `THROTTLE_COUNTERS_UNREADABLE` | yes | No throttle counter readable, so throttling was not ruled out. |
| `AFFINITY_UNSUPPORTED` | yes | No pinning mechanism, so a thread may have migrated. |
| `GOVERNOR_UNKNOWN` | yes | The governor could not be read. |
| `GOVERNOR_NOT_PERFORMANCE` | no | The governor was not `performance`, or the target CPUs did not share one. |
| `MAJOR_FAULTS_DURING_RUN` | no | The process took major page faults during the run. |
| `RUN_TOO_SHORT` | no | The timed region was short relative to the clock resolution. |
| `TIMER_RESOLUTION_COARSE` | no | The clock resolution is coarse relative to the timed region. |
| `HIGH_RUN_VARIANCE` | no | Repeat timings disagreed by more than the tolerated spread. |

**Informational**, recorded and never blocking:

| code | meaning |
| --- | --- |
| `THEORETICAL_PEAK_UNKNOWN` | The memory configuration was unreadable, so the peak and the fraction are omitted rather than guessed. |
| `NUMPY_PATH_OVERHEAD` | The figure came from the numpy path, so it is a lower bound on a fused compiled kernel. |
| `C_PATH_UNAVAILABLE` | The compiled kernel was not available, so the numpy path was used. |
| `PER_CLUSTER_UNAVAILABLE` | Cluster topology could not be determined, so only an aggregate figure is reported. |

### Byte conventions

Stated on every figure, because they are the difference between two numbers
that look comparable and are not:

| kernel | bytes counted | convention |
| --- | --- | --- |
| read | `n * 8` | one array read |
| copy | `2 * n * 8` | STREAM Copy: one array read plus one written |
| triad | `3 * n * 8` | STREAM Triad: two arrays read plus one written |
| strided read | `lines * 64` | cache lines fetched, **not** comparable with the sequential read |

## How the theoretical peak is derived

One formula, stated in every record:

```
peak_GB_s = data_rate_MT_s * total_data_width_bits / 8 / 1000
```

`total_data_width_bits` is the sum of the **data** width over the populated
memory modules, so ECC check bits are excluded. `data_rate_MT_s` is the
configured transfer rate, preferred over the rated one, because the
configured rate is what the controller is running at. If populated modules
disagree on the rate, the lowest is used and the disagreement is recorded.
`GB` is decimal, 10^9 bytes, which is the convention memory vendors use when
they quote a peak, so the fraction compares like with like.

Two worked examples:

- two DDR5-4800 modules of 64 data bits each: `4800 * 128 / 8 / 1000 = 76.8`
  GB/s
- one LPDDR4X-4267 32-bit channel: `4267 * 32 / 8 / 1000 = 17.068` GB/s

Sources, in the order they are tried:

1. `--memory-config RATE:WIDTH:MODULES` or `MEMBW_MEMORY_CONFIG`, recorded as
   an operator override rather than a reading.
2. On Linux, `dmidecode -t memory` (SMBIOS). Usually needs root.
3. On Windows, `Win32_PhysicalMemory` through CIM. Needs no administrator
   rights.
4. On Arm, the device tree. Most device trees, the Raspberry Pi's included,
   publish the memory size and nothing about the transfer rate or the bus
   width, so this path usually reports `UNKNOWN` and names the nodes it
   looked at. Where a device tree gives a clock frequency, `membw` records
   it and does **not** use it: a clock is not a transfer rate, and inferring
   a DDR multiplier that nobody stated is a factor-of-two error in the
   denominator.

When no source yields both a rate and a width, the peak is `UNKNOWN`, the
achieved fraction is omitted, and `THEORETICAL_PEAK_UNKNOWN` is recorded.
`membw` never substitutes a datasheet figure for a part it recognises.
`/proc/meminfo` contributes capacity only, never a rate.

## Why per cluster

On a heterogeneous SoC the clusters usually share one memory controller, and
one cluster can nearly saturate it on its own. The author's Galaxy M21
measurements above are the case in point: the A73 cluster alone reaches 7.51
GB/s, adding four A53 cores takes it to 7.84 GB/s, a 4 percent gain, and
A53-only is 4.57 GB/s. A single aggregate figure of 7.84 GB/s hides all
three facts, and a reader cannot tell from it whether adding little cores to
a memory-bound kernel would help.

So `membw` measures each cluster on its own as well as in aggregate, with
the same array size throughout so the figures are comparable, and reports
the aggregate's gain over the best single cluster. It states the arithmetic
and leaves the conclusion to the reader.

Clusters are detected from the first of these that distinguishes the CPUs:

1. `/proc/cpuinfo` `CPU implementer` and `CPU part`, which is the robust
   big.LITTLE discriminator on Arm. The stepping is recorded but is not part
   of the grouping key, because a stepping difference is not a different
   cluster.
2. `/sys/devices/system/cpu/cpu*/cpu_capacity`.
3. On Windows, `GetLogicalProcessorInformationEx`
   `PROCESSOR_RELATIONSHIP.EfficiencyClass`, which separates performance
   from efficient cores on a hybrid x86 part.

If none distinguishes them, `membw` reports one cluster and records
`PER_CLUSTER_UNAVAILABLE` rather than implying it checked.

Each worker thread is pinned to exactly one CPU before it touches any
memory, so pages are first touched by the thread that will read them. The
mask is then read back; the mask in the record is always the effective one.
Each worker additionally samples which CPU it is running on, which catches a
migration that a mask read-back alone would not.

## Working set and the cache margin

Each array is sized to at least `--llc-margin` times the summed size of the
last-level caches reachable from the CPUs under test, with a floor of one
million elements. The default margin of 4 is taken from STREAM's run rules,
which require each array to be at least four times the sum of all the
last-level caches used in the run, so a `membw` buffer is never smaller than
a conforming STREAM buffer.

`membw` enforces that rule in code. If available memory will not allow it,
the figure is refused with `WORKING_SET_BELOW_LLC` or
`MEMORY_INSUFFICIENT`. If the cache size cannot be read at all, the arrays
fall back to a 64 MiB floor and the verdict is `UNKNOWN`, never `PASS`.

A single pass over such a buffer takes only a few milliseconds, which is
short enough for the clock and the scheduler to show up in the number, so
`membw` times several passes per repetition and divides. The pass count is
calibrated from one timed pass to reach `--min-run-seconds`, and it is
recorded. Re-reading a buffer already several times larger than the cache
does not make this a cache measurement: by the time the second pass starts,
the beginning of the buffer has long been evicted.

## Governor handling

`membw` reads the governor before and after the run and refuses the figure if
it changed. It does **not** change the governor itself unless explicitly
asked with `--set-governor NAME`, which is Linux only and needs write access
to sysfs. When asked, it records the previous value per CPU, restores it
afterwards, reads it back to confirm, and reports
`GOVERNOR_RESTORE_FAILED` as invalidating if the restore cannot be verified.
A tool that leaves a machine in a changed power state must say so.

On Windows there is no cpufreq governor. `membw` records the active power
plan instead, names `GOVERNOR_UNKNOWN`, and lists the governor as
unreadable, which makes the verdict `UNKNOWN`. That is the honest outcome:
the clock policy under which the figure was taken is not recorded in a form
comparable with a Linux run.

## Related work

Verified against primary sources (project pages, READMEs, man pages, source)
in November 2026. The scorecard is against the four things `membw` claims:
a machine-readable provenance record, per-cluster reporting, validity
refusal, and a CI surface.

**STREAM** (John D. McCalpin, University of Virginia) is the reference point
and does the measurement better than this tool will. Its licence is a
bespoke notice rather than an SPDX licence, and it is specific: results may
be published, but to be called "STREAM benchmark results" they must conform
to the STREAM Run Rules, results from modified source or non-conforming runs
must be clearly labelled, and the copyright holder retains the right to
determine conformity. `membw` therefore describes its Copy and Triad as
STREAM-convention or STREAM-style and never as STREAM results. The Run Rules
state the array-size rule `membw` enforces: each array at least four times
the sum of all the last-level caches used in the run, with a floor of one
million elements. STREAM states that rule in prose for a human to obey and
ships no code to check it. Its own `checkSTREAMresults` validates the
kernels' arithmetic, which is a different thing from validating the
experimental conditions. Output is plain text, with no affinity mask,
governor, compiler or hardware identification. Cite McCalpin 1995, IEEE
TCCA Newsletter, "Memory Bandwidth and Machine Balance in Current High
Performance Computers".

**likwid-bench** (RRZE-HPC/likwid, GPL-3.0) is the closest existing tool on
pinning, and it is good at it: a workgroup syntax targets a thread domain,
placement is compact and documented, and the output names the core each
thread landed on. It is the nearest neighbour to per-cluster reporting and
does not quite get there: domains are sockets, shared caches and memory
domains, the documentation shows no per-domain breakdown of the bandwidth
metric, and bandwidth is computed from the maximum runtime across all
threads, which is exactly the aggregation that flattens a big.LITTLE number.
Its own documentation warns that unequal workgroups leave some threads
finishing early. It does no validity refusal.

**Intel Memory Latency Checker (MLC)** is thorough and is not open source:
it ships as a binary under Intel's Memory Latency Tool License Agreement,
with a personal, non-transferable, revocable grant. It cannot be vendored
into an open-source tool or a GitHub Action, and a patched build cannot be
shipped. It is Intel-focused, documents Windows and Linux, and emits
plain-text matrices with no provenance record and no refusal.

**sysbench memory** (GPL-2.0) should not be used as a bandwidth ceiling, and
its own source says why: the default block size is 1 KiB and the default
scope is a single shared buffer, so at defaults the reported MiB/s is an L1
store rate published under the word "memory". The code never queries a cache
size and does no pinning. It is the clearest example of the failure mode
`membw`'s working-set refusal exists to catch.

**mbw** (GPL-3.0) measures copy bandwidth with three methods and is honest
about its scope: its README calls the approach simplistic, says it is not
aware of hardware architecture, and warns about swap. It is
single-threaded, does not pin, never compares the buffer to a cache size,
and emits tab-separated text.

**lmbench `bw_mem`** allocates twice the requested memory and times a copy of
one half into the other, in several modes, and its man page is candid that
the benchmark can move up to three times the requested memory and that
`bcopy` uses two to three times the bandwidth of the data it copies.
Upstream is effectively dormant, with the newest upstream release being
3.0-a9 and distributions carrying their own patched builds. No provenance,
no pinning documented, no refusal. Its licence was not confirmed from the
tarball, so no claim is made about it here.

**tinymembench** (MIT) measures peak sequential bandwidth and random-access
latency with hand-written assembly across aarch64, ARM NEON, MIPS-32 and
x86 SSE2, trying several prefetch strategies. It is widely used on Arm. It
is single-threaded, does not pin, uses a fixed 32 MiB buffer rather than one
derived from the cache, reads and prints no CPU topology, and emits plain
text. On a big.LITTLE part an unpinned single-threaded run can be migrated
between a big and a little core mid-measurement and the tool will not say
so. The last code push was in 2023.

**pmbw** (GPL-3.0) sweeps growing array sizes and growing thread counts with
hand-written assembler across several transfer widths, which is the closest
existing thing to finding a ceiling by sweeping, and is worth citing for
that. It is essentially unmaintained, and no claim is made here about its
output format, which was not confirmed.

**BabelStream** (University of Bristol HPC) is an actively developed
STREAM-in-spirit benchmark across many device programming models, and it
does verify its results numerically. It is device-oriented, its per-device
reporting is a GPU notion rather than a CPU cluster notion, and it publishes
no reusable Action. Its licence, output format and pinning behaviour were
not confirmed from source, so nothing further is claimed about it.

**criteo/hwbench** (Apache-2.0) is the nearest neighbour to the provenance
record and is the one to acknowledge most carefully. It attaches rich
hardware and environmental context to results as JSON, including BIOS and
DMI information and thermal, power and fan traces. It does not benchmark
memory bandwidth, it records the environment rather than invalidating a run
because of it, and it publishes neither a PyPI package nor an Action.

**Python options.** No pip-installable package that measures memory
bandwidth was found. The closest PyPI hit, `pytest-benchmem`, measures
memory allocation rather than throughput. `lukeolson/stream_python` on
GitHub is the only Python STREAM implementation found; it ships pure Python,
numpy, Cython and pybind11 implementations, does not size arrays relative to
cache, and reports its numpy figure alongside its pure-Python figure without
remarking on the difference. That is the hazard behind the Raspberry Pi 5
discrepancy above, and it is not documented anywhere that could be found.
Note that this survey rests on name probes and web search rather than an
exhaustive index scan, because PyPI's search page could not be read
programmatically.

**GitHub Actions.** No Marketplace Action was found that measures a CI
runner's memory bandwidth. Marketplace searches for "memory bandwidth" and
"benchmark hardware" returned nothing, and every Action returned for
"memory" measures the software being built rather than the host. Adjacent
tools exist: CodSpeed tracks allocations and heap, and `attunehq/caliper`
times builds under different CPU and RAM configurations to right-size
runners. Neither reports bandwidth. A keyword search cannot prove a
negative across every unlisted composite action, so the claim here is that
none is known, not that none exists.

### What membw adds

No existing tool does all four of the following, and none found does even
three.

1. **A provenance record.** A machine-readable record per run naming the
   code path, the affinity mask actually in effect, the governor, the
   working set against the last-level cache, the theoretical-peak formula
   and its inputs, the thresholds the verdict was reached under, and every
   source that could not be read.
2. **Per-cluster reporting**, with the aggregate stated against the best
   single cluster.
3. **Validity refusal.** No tool surveyed refuses a run. Those that check
   anything check their kernels' arithmetic, which is correctness, not
   validity. None reads cgroup quota, thermal counters or competing load and
   declines to report.
4. **A CI surface**, as an Action at the repository root.

The defensible claim is narrow and still true: existing tools treat run
validity and run provenance as the operator's responsibility, stated in
prose and enforced by nobody. STREAM writes the four-times-cache rule into
its Run Rules and ships no code to check it. sysbench's source never queries
a cache size. tinymembench and mbw do not pin, so a big.LITTLE migration is
invisible. likwid-bench pins well, names the core, then collapses
heterogeneous threads into one maximum-runtime figure and warns you about
it. hwbench collects machine context as JSON and benchmarks no bandwidth.
MLC's licence forecloses it from open tooling. And nobody has put any of it
in CI.

## Related tools

`membw` is one of three sibling tools that emit the same
`measurement-provenance/1` envelope, so their records interoperate:

- `membw` measures achieved memory bandwidth against theoretical peak.
- `benchguard` decides whether a measurement was valid, from cgroup
  throttling, thermal counters, governor, major faults and competing load.
  It owns the envelope's verdict vocabulary.
- `powerstamp` reports energy with its measured sampling rate and
  uncertainty.

A consumer that can read one tool's envelope can read any of them: the
verdict, the reasons and the unreadable-source list are in the same place
with the same meanings, and only the `measurement` payload differs. Each
tool documents its own `reasons[].code` values; `membw`'s are tabulated
above.

## Limitations

- **The byte conventions count what the program asked for, not what the DRAM
  moved.** On a machine with a write-allocate cache, the write in Copy also
  fetches the line it is about to overwrite, so true DRAM traffic for Copy
  can be three arrays rather than two. The convention is STREAM's, kept so
  the numbers stay comparable, and it is stated on every figure. If you want
  DRAM-side traffic, read the memory controller's counters.
- **The ceiling's kernel name is load-bearing.** A multi-threaded pure read
  has no write turnaround and no read-for-ownership traffic, so on some
  memory systems it lands well above STREAM Copy and Triad on the same
  machine. `ceiling_gb_s` is the highest of the three and
  `ceiling_kernel` says which one it was. A ceiling from the `read` kernel
  and a ceiling from the `triad` kernel are not interchangeable
  denominators, and quoting one against a STREAM-derived figure without
  saying which kernel produced it is the reporting defect this tool is
  meant to remove. Pick the kernel whose access pattern matches the code
  you are analysing.
- **The numpy path is a lower bound, not an equal alternative.** numpy
  cannot express a fused `a = b + q*c`, so the numpy triad is two ufunc
  passes that move five arrays' worth of traffic while being credited with
  STREAM's three. Every kernel on that path also carries numpy per-call
  overhead. The path is recorded on every figure and the run always carries
  `NUMPY_PATH_OVERHEAD`.
- **The compiled kernels are plain C with no SIMD intrinsics and no
  non-temporal stores**, and are built without `-ffast-math`. Those would
  raise the number on some machines and change what is being measured. The
  compiler and flags are recorded, so a figure from a differently built
  library is never silently compared with one from this build.
- **Windows reports less.** There is no cpufreq governor, no cgroup quota
  counter and no per-CPU thermal throttle counter to read, and the pre-run
  busy fraction comes from `GetSystemTimes`, which is whole-system rather
  than per target CPU and therefore understates a single busy core. A
  Windows run will normally come back `UNKNOWN` for that reason, and a
  Windows figure is not comparable with a Linux figure from the same
  hardware.
- **Pinning on Windows is limited to a single 64-CPU processor group.**
  `SetThreadGroupAffinity` is not used yet, so a request naming a CPU at or
  above 64 is refused rather than silently narrowed.
- **The theoretical peak is a bus-width times transfer-rate figure.** It is
  the number vendors quote and the one a roofline is usually drawn against.
  It is not a model of the controller, so it does not account for refresh,
  bank conflicts or read/write turnaround, which is part of why no real
  machine reaches it.
- **NUMA is not modelled.** Buffers are first touched by the pinned thread
  that will use them, which gets local placement on a NUMA machine, but
  `membw` does not sweep node-to-node combinations the way Intel MLC does.
- **Hugepages are not requested.** Transparent hugepages, if the system
  enables them, will apply; `membw` neither asks for them nor reports them
  as a configured input beyond the `/proc/meminfo` hugepage size.
- **A refusal is not a diagnosis.** `membw` says the figure is untrustworthy
  and names the condition. Deciding what to do about a throttling runner is
  the operator's problem.
- **`membw` has no adoption to report.** It is newly written and unreleased.
  Nothing here claims otherwise.

## Development

```sh
pip install -e ".[dev]"
python -m pytest -v
python -m membw.cli info
python -m membw.cli schema
```

The test suite deliberately does not assert any throughput value. It
exercises the parsing, the pinning logic, the buffer-sizing and margin
arithmetic, the refusal rules, the envelope shape, the CLI exit codes, and
the consistency of this README with the code's reason-code registry. The
only numeric assertions on a measurement are that a bandwidth is positive
and finite.

The repository is pure ASCII. CI enforces it with
`LC_ALL=C grep -rn '[^ -~]' .` and the test suite checks it again.

## License and citation

MIT. Copyright (c) 2026 Manu Nicholas Jacob.

Author: Manu Nicholas Jacob, Independent Researcher, ORCID
[0009-0007-6589-6572](https://orcid.org/0009-0007-6589-6572).

See `CITATION.cff` for machine-readable citation metadata.
