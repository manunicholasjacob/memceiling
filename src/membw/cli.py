"""The membw command line.

    membw                       measure, print a human summary
    membw --json                measure, print the provenance envelope
    membw info                  report what can be read, measure nothing
    membw build-c               build the optional compiled kernels
    membw schema                print the reason-code vocabulary

Exit status is the verdict, so a CI step can act on it:

    0  PASS            the figure is trustworthy
    2  INVALID         conditions during the run invalidate the figure
    3  UNKNOWN         membw could not establish validity
    4  UNSUPPORTED     membw cannot measure here
    1  an internal error, with a traceback

Pass --exit-zero to record a figure in CI without failing the build on a
non-PASS verdict. The verdict is still in the JSON either way.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import schema as _schema
from .cbuild import build_c_kernels, default_library_path, detect_compiler
from .kernels import DEFAULT_LLC_MARGIN, DEFAULT_MEMORY_FRACTION
from .run import CODE_PATH_AUTO, MeasureOptions, measure
from .theoretical import FORMULA, derive_theoretical_peak
from .topology import detect_topology, format_cpu_list, parse_cpu_list
from .validity import (
    DEFAULT_BUSY_THRESHOLD,
    DEFAULT_MIN_RUN_SECONDS,
    DEFAULT_SPREAD_TOLERANCE,
)
from .version import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2
EXIT_UNKNOWN = 3
EXIT_UNSUPPORTED = 4

EXIT_FOR_VERDICT = {
    _schema.VERDICT_PASS: EXIT_OK,
    _schema.VERDICT_INVALID: EXIT_INVALID,
    _schema.VERDICT_UNKNOWN: EXIT_UNKNOWN,
    _schema.VERDICT_UNSUPPORTED: EXIT_UNSUPPORTED,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="membw",
        description=(
            "Measure the achieved memory bandwidth ceiling of this machine "
            "and report it against the theoretical peak, with a record of "
            "the conditions the figure was taken under."
        ),
        epilog=(
            "membw refuses to present a figure taken under conditions that "
            "invalidate it. A non-zero exit status is the tool working."
        ),
    )
    parser.add_argument("--version", action="version", version="membw " + __version__)

    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="take a measurement (the default)")
    _add_run_arguments(run_parser)

    info_parser = sub.add_parser(
        "info", help="report readable topology and theoretical peak, measure nothing"
    )
    info_parser.add_argument("--json", action="store_true", help="print JSON")
    info_parser.add_argument(
        "--memory-config",
        metavar="RATE:WIDTH:MODULES",
        help="override the memory configuration used for the theoretical peak",
    )

    build_parser = sub.add_parser("build-c", help="build the compiled kernel library")
    build_parser.add_argument(
        "--force", action="store_true", help="rebuild even if a cached library exists"
    )
    build_parser.add_argument("--output", metavar="PATH", help="write the library here")
    build_parser.add_argument("--json", action="store_true", help="print JSON")

    schema_parser = sub.add_parser(
        "schema", help="print the envelope shape and the reason-code vocabulary"
    )
    schema_parser.add_argument("--json", action="store_true", help="print JSON")

    # Allow the run flags with no subcommand at all.
    _add_run_arguments(parser)
    return parser


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the full envelope")
    parser.add_argument(
        "--out", metavar="PATH", help="also write the envelope as JSON to PATH"
    )
    parser.add_argument(
        "--cpus",
        metavar="LIST",
        help="CPUs to measure on, as a list such as 0-7,12 (default: all online)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        metavar="N",
        help="worker threads (default: one per selected CPU)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        metavar="N",
        help="timed repetitions per kernel, best is reported (default: 5)",
    )
    parser.add_argument(
        "--code-path",
        choices=(CODE_PATH_AUTO, "c", "numpy"),
        default=CODE_PATH_AUTO,
        help=(
            "which kernels to use. 'auto' prefers the compiled path and falls "
            "back to numpy; 'c' refuses to run without it (default: auto)"
        ),
    )
    parser.add_argument(
        "--c-library",
        metavar="PATH",
        help="use this prebuilt kernel library instead of building one",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="do not try to compile the kernel library if it is missing",
    )
    cluster_group = parser.add_mutually_exclusive_group()
    cluster_group.add_argument(
        "--per-cluster",
        dest="per_cluster",
        action="store_true",
        default=None,
        help="measure each CPU cluster separately as well as the aggregate",
    )
    cluster_group.add_argument(
        "--no-per-cluster",
        dest="per_cluster",
        action="store_false",
        help="measure only the aggregate, even on a heterogeneous machine",
    )
    parser.add_argument(
        "--llc-margin",
        type=float,
        default=DEFAULT_LLC_MARGIN,
        metavar="X",
        help=(
            "each array must be at least X times the summed last-level cache "
            "(default: %.1f, which is STREAM's run rule)" % DEFAULT_LLC_MARGIN
        ),
    )
    parser.add_argument(
        "--bytes-per-array",
        type=int,
        metavar="N",
        help="set the array size explicitly, overriding the margin rule",
    )
    parser.add_argument(
        "--memory-fraction",
        type=float,
        default=DEFAULT_MEMORY_FRACTION,
        metavar="F",
        help=(
            "largest share of available memory membw will occupy "
            "(default: %.2f)" % DEFAULT_MEMORY_FRACTION
        ),
    )
    parser.add_argument(
        "--memory-config",
        metavar="RATE:WIDTH:MODULES",
        help=(
            "supply the memory configuration for the theoretical peak, for "
            "example 4267:32:1. Recorded as an operator override, not a read."
        ),
    )
    parser.add_argument(
        "--include-strided",
        action="store_true",
        help="also measure a cache-line strided read (a different convention)",
    )
    parser.add_argument(
        "--busy-threshold",
        type=float,
        default=DEFAULT_BUSY_THRESHOLD,
        metavar="F",
        help=(
            "pre-run CPU busy fraction above which the run is invalid "
            "(default: %.2f)" % DEFAULT_BUSY_THRESHOLD
        ),
    )
    parser.add_argument(
        "--spread-tolerance",
        type=float,
        default=DEFAULT_SPREAD_TOLERANCE,
        metavar="F",
        help=(
            "tolerated spread between repeat timings (default: %.2f)"
            % DEFAULT_SPREAD_TOLERANCE
        ),
    )
    parser.add_argument(
        "--min-run-seconds",
        type=float,
        default=DEFAULT_MIN_RUN_SECONDS,
        metavar="S",
        help=(
            "shortest timed region treated as stable (default: %.3f)"
            % DEFAULT_MIN_RUN_SECONDS
        ),
    )
    parser.add_argument(
        "--set-governor",
        metavar="NAME",
        help=(
            "OPT IN: set the CPU frequency governor to NAME for the run, then "
            "restore the previous value and verify the restore. Linux only, "
            "needs write access to sysfs. Off by default: membw does not "
            "change your machine's power state unless you ask."
        ),
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="always exit 0; the verdict is still reported in the output",
    )


def _options_from_args(args) -> MeasureOptions:
    return MeasureOptions(
        cpus=list(parse_cpu_list(args.cpus)) if getattr(args, "cpus", None) else None,
        per_cluster=args.per_cluster,
        code_path=args.code_path,
        reps=args.repeats,
        threads=args.threads,
        llc_margin=args.llc_margin,
        bytes_per_array=args.bytes_per_array,
        memory_fraction=args.memory_fraction,
        memory_config=args.memory_config,
        include_strided=args.include_strided,
        busy_threshold=args.busy_threshold,
        spread_tolerance=args.spread_tolerance,
        min_run_seconds=args.min_run_seconds,
        set_governor=args.set_governor,
        c_library=args.c_library,
        allow_c_build=not args.no_build,
    )


def _fmt(value, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "UNKNOWN"
    return ("%." + str(digits) + "f%s") % (value, suffix)


def format_human(result) -> str:
    """Render a Result as the terminal summary."""
    envelope = result.envelope
    measurement = envelope.get("measurement") or {}
    achieved = measurement.get("achieved") or {}
    peak = measurement.get("theoretical_peak") or {}
    topo = measurement.get("topology") or {}
    aggregate = measurement.get("aggregate") or {}
    conditions = measurement.get("conditions") or {}
    code_path = measurement.get("code_path") or {}
    buffers = (aggregate.get("buffers") or {}) if aggregate else {}

    lines = []
    lines.append("membw %s   verdict: %s" % (envelope["tool"]["version"], envelope["verdict"]))
    lines.append("taken %s on %s %s (%s)" % (
        envelope["taken_at_utc"],
        envelope["platform"]["system"],
        envelope["platform"]["release"],
        envelope["platform"]["machine"],
    ))
    if topo.get("cpu_model"):
        lines.append("cpu   %s" % topo["cpu_model"])
    lines.append("")

    lines.append("achieved bandwidth (code path: %s)" % (code_path.get("used") or "none"))
    lines.append("  read              %s GB/s" % _fmt(achieved.get("read_gb_s")))
    lines.append("  copy  (STREAM)    %s GB/s" % _fmt(achieved.get("copy_gb_s")))
    lines.append("  triad (STREAM)    %s GB/s" % _fmt(achieved.get("triad_gb_s")))
    if achieved.get("strided_read_gb_s") is not None:
        lines.append(
            "  strided read      %s GB/s  (cache-line convention)"
            % _fmt(achieved.get("strided_read_gb_s"))
        )
    lines.append(
        "  ceiling           %s GB/s  (from the %s kernel)"
        % (_fmt(achieved.get("ceiling_gb_s")), achieved.get("ceiling_kernel") or "?")
    )
    lines.append("")

    lines.append("theoretical peak    %s GB/s  [%s]" % (
        _fmt(peak.get("peak_gb_s")), peak.get("status", "UNKNOWN")
    ))
    if peak.get("source"):
        lines.append("  source            %s" % peak["source"])
    lines.append("  formula           %s" % FORMULA)
    inputs = peak.get("inputs") or {}
    if inputs:
        lines.append(
            "  inputs            %s MT/s x %s bits / 8 / 1000"
            % (inputs.get("data_rate_mt_s"), inputs.get("total_data_width_bits"))
        )
    if achieved.get("fraction_of_theoretical_percent") is not None:
        lines.append(
            "  achieved fraction %s%% of theoretical"
            % _fmt(achieved.get("fraction_of_theoretical_percent"))
        )
    lines.append("")

    affinity = (aggregate.get("affinity") or {}) if aggregate else {}
    lines.append("configuration")
    lines.append("  threads           %s" % aggregate.get("threads", "?"))
    lines.append("  requested cpus    %s" % (affinity.get("requested_cpu_list") or "?"))
    lines.append("  effective mask    %s%s" % (
        affinity.get("observed_cpu_list") or "unreadable",
        "" if affinity.get("every_thread_as_requested") else "  (NOT as requested)",
    ))
    lines.append("  clusters          %d from %s" % (
        len(topo.get("clusters") or []), topo.get("cluster_source", "?")
    ))
    lines.append("  governor          %s" % (
        conditions.get("governor")
        or ("power plan: %s" % conditions["windows_power_plan"]
            if conditions.get("windows_power_plan")
            else "UNKNOWN")
    ))
    if buffers:
        lines.append("  array size        %.1f MiB each, %d arrays (%s)" % (
            buffers.get("bytes_per_array", 0) / 1048576.0,
            buffers.get("arrays", 0),
            "margin satisfied" if buffers.get("satisfies_margin") else "MARGIN NOT VERIFIED",
        ))
        lines.append("  last-level cache  %s" % (
            "%.1f MiB (level %s)" % (
                buffers["llc_total_bytes"] / 1048576.0,
                topo.get("llc", {}).get("level"),
            )
            if buffers.get("llc_total_bytes")
            else "UNKNOWN"
        ))

    per_cluster = measurement.get("cluster_saturation")
    if per_cluster:
        lines.append("")
        lines.append("per cluster")
        for row in per_cluster.get("per_cluster") or []:
            lines.append("  %-22s %s GB/s  cpus %s%s" % (
                row["cluster"],
                _fmt(row["ceiling_gb_s"]),
                row["cpu_list"],
                ""
                if row.get("fraction_of_theoretical_percent") is None
                else "  (%s%% of theoretical)" % _fmt(row["fraction_of_theoretical_percent"]),
            ))
        gain = per_cluster.get("aggregate_gain_over_best_cluster_percent")
        if gain is not None:
            lines.append(
                "  aggregate over the best single cluster: %s%%" % _fmt(gain)
            )

    reasons = envelope.get("reasons") or []
    if reasons:
        lines.append("")
        lines.append("reasons")
        for item in reasons:
            lines.append("  [%s] %s" % (item["severity"], item["code"]))
            lines.append("      %s" % item["detail"])

    unreadable = envelope.get("unreadable") or []
    if unreadable:
        lines.append("")
        lines.append("could not read (%d)" % len(unreadable))
        for item in unreadable:
            lines.append("  - %s" % item)

    return "\n".join(lines)


def _cmd_run(args) -> int:
    options = _options_from_args(args)
    result = measure(options)

    text = result.to_json() if args.json else format_human(result)
    sys.stdout.write(text + "\n")

    if args.out:
        with open(args.out, "w", encoding="ascii") as handle:
            handle.write(result.to_json() + "\n")
        if not args.json:
            sys.stdout.write("\nenvelope written to %s\n" % args.out)

    if args.exit_zero:
        return EXIT_OK
    return EXIT_FOR_VERDICT.get(result.verdict, EXIT_ERROR)


def _cmd_info(args) -> int:
    topo = detect_topology()
    peak = derive_theoretical_peak(args.memory_config)
    kind, exe = detect_compiler()
    payload = {
        "topology": topo.as_dict(),
        "theoretical_peak": peak.as_dict(),
        "compiler": {"kind": kind, "path": exe},
        "cached_c_library": default_library_path(),
        "unreadable": topo.unreadable + peak.unreadable,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return EXIT_OK

    lines = ["membw info (nothing was measured)", ""]
    lines.append("cpu model        %s" % (topo.cpu_model or "UNKNOWN"))
    lines.append("online cpus      %s (%d)" % (
        format_cpu_list(topo.online_cpus), len(topo.online_cpus)
    ))
    lines.append("clusters         %d from %s" % (len(topo.clusters), topo.cluster_source))
    for cluster in topo.clusters:
        lines.append("  %-22s cpus %s" % (cluster.name, format_cpu_list(cluster.cpus)))
    lines.append("last-level cache %s" % (
        "%.1f MiB (level %s, %s instance(s))" % (
            topo.llc_total_bytes / 1048576.0, topo.llc_level, topo.llc_instances
        )
        if topo.llc_total_bytes
        else "UNKNOWN"
    ))
    lines.append("memory           %s" % (
        "%.1f GiB total, %.1f GiB available" % (
            (topo.total_ram_bytes or 0) / 1073741824.0,
            (topo.available_ram_bytes or 0) / 1073741824.0,
        )
        if topo.total_ram_bytes
        else "UNKNOWN"
    ))
    lines.append("theoretical peak %s GB/s [%s]" % (
        _fmt(peak.peak_gb_s), "KNOWN" if peak.known else "UNKNOWN"
    ))
    lines.append("  formula        %s" % FORMULA)
    if peak.source:
        lines.append("  source         %s" % peak.source)
    lines.append("c compiler       %s" % (("%s at %s" % (kind, exe)) if exe else "none found"))
    lines.append("c library cache  %s" % default_library_path())
    unreadable = payload["unreadable"]
    if unreadable:
        lines.append("")
        lines.append("could not read (%d)" % len(unreadable))
        for item in unreadable:
            lines.append("  - %s" % item)
    sys.stdout.write("\n".join(lines) + "\n")
    return EXIT_OK


def _cmd_build_c(args) -> int:
    report = build_c_kernels(output_path=args.output, force=args.force)
    if args.json:
        sys.stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
    elif report.ok:
        sys.stdout.write(
            "built %s\n  compiler %s\n  flags %s\n"
            % (report.library_path, report.compiler, " ".join(report.flags))
        )
    else:
        sys.stdout.write("build failed: %s\n" % report.error)
        if report.stdout:
            sys.stdout.write("--- compiler stdout ---\n%s\n" % report.stdout.strip())
        if report.stderr:
            sys.stdout.write("--- compiler stderr ---\n%s\n" % report.stderr.strip())
        sys.stdout.write(
            "\nmembw will use the numpy path instead, and will label every "
            "figure accordingly.\n"
        )
    return EXIT_OK if report.ok else EXIT_UNSUPPORTED


def _cmd_schema(args) -> int:
    payload = {
        "schema": _schema.SCHEMA_ID,
        "tool": _schema.TOOL_NAME,
        "verdicts": list(_schema.VERDICTS),
        "severities": list(_schema.SEVERITIES),
        "confidence_blocking_codes": sorted(_schema.CONFIDENCE_BLOCKING),
        "reason_codes": {
            code: {"severity": severity, "meaning": meaning}
            for code, (severity, meaning) in sorted(_schema.REASON_CODES.items())
        },
        "verdict_rule": [
            "PLATFORM_UNSUPPORTED present -> UNSUPPORTED",
            "any reason of severity 'invalidating' -> INVALID",
            "any confidence-blocking warning -> UNKNOWN",
            "otherwise -> PASS",
        ],
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return EXIT_OK
    lines = [
        "envelope schema  %s" % payload["schema"],
        "verdicts         %s" % ", ".join(payload["verdicts"]),
        "",
        "verdict rule",
    ]
    for step in payload["verdict_rule"]:
        lines.append("  %s" % step)
    lines.append("")
    lines.append("reason codes")
    for code, info in payload["reason_codes"].items():
        blocking = " (confidence-blocking)" if code in _schema.CONFIDENCE_BLOCKING else ""
        lines.append("  %-32s %s%s" % (code, info["severity"], blocking))
        lines.append("      %s" % info["meaning"])
    sys.stdout.write("\n".join(lines) + "\n")
    return EXIT_OK


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None) or "run"
    if command == "run":
        return _cmd_run(args)
    if command == "info":
        return _cmd_info(args)
    if command == "build-c":
        return _cmd_build_c(args)
    if command == "schema":
        return _cmd_schema(args)
    parser.error("unknown command %r" % command)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
