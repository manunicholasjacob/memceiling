# Changelog

All notable changes to memceiling are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-11-01

First release. Not yet published to PyPI or tagged on GitHub; see
`RELEASE_CHECKLIST.md` for the steps that remain.

### Added

- `measure()` and the `MeasureOptions` / `Result` pair, the whole library API
  most callers need.
- Achieved sequential read, STREAM-convention Copy and Triad, and an
  optional cache-line strided read, each labelled with its byte convention.
- Theoretical peak derived from the readable memory configuration via
  `peak_GB_s = data_rate_MT_s * total_data_width_bits / 8 / 1000`, reported
  as UNKNOWN rather than guessed when no source yields both a rate and a
  width. Sources: an explicit operator override, `dmidecode -t memory` on
  Linux, `Win32_PhysicalMemory` on Windows, and the Arm device tree.
- Per-cluster measurement alongside the aggregate, with cluster detection
  from Arm `CPU implementer` and `CPU part`, from `cpu_capacity`, or from
  the Windows `PROCESSOR_RELATIONSHIP.EfficiencyClass`, plus the aggregate's
  gain over the best single cluster.
- Explicit thread pinning with the mask read back afterwards, so the record
  reports the mask actually in effect, and per-worker sampling of the CPU
  actually run on.
- Validity refusal with a documented reason-code vocabulary and a four-value
  verdict: PASS, INVALID, UNKNOWN, UNSUPPORTED. Refuses on a working set
  inside the last-level cache, competing load, thermal or cgroup quota
  throttling during the run, swap activity, a governor change across the
  run, an affinity mask that did not take, and an achieved figure above
  theoretical.
- Two code paths, a compiled C kernel library loaded with ctypes and a
  portable numpy path, with the path recorded on every figure and the numpy
  path always labelled as a lower bound.
- Auto-calibrated pass count per timed repetition, so the timed region
  reaches a configurable minimum duration.
- Governor control behind an explicit `--set-governor` opt-in, with the
  previous value restored and the restore verified.
- The `measurement-provenance/1` output envelope, shared with the sibling
  tools named in the README, including an `unreadable` list naming every
  source that could not be read.
- CLI: `memceiling run` (the default), `memceiling info`, `memceiling build-c`,
  `memceiling schema`, with `--json`, `--out` and the verdict as the exit status.
- `action.yml` at the repository root, so the Action can be used as
  `manunicholasjacob/memceiling@v0.1.0` to record a CI runner's real memory
  ceiling into the build.
- A test suite that asserts no throughput value: parsing, pinning logic,
  buffer-sizing arithmetic, refusal rules, envelope shape, CLI exit codes,
  and the README's coverage of the reason-code vocabulary.

[Unreleased]: https://github.com/manunicholasjacob/memceiling/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/manunicholasjacob/memceiling/releases/tag/v0.1.0
