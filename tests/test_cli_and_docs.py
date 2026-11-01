"""CLI behaviour and documentation consistency.

The documentation tests are here deliberately. A provenance tool whose
README does not define its own reason codes is not providing provenance, so
the coverage of the vocabulary by the README is enforced, not trusted. The
ASCII test enforces a hard project rule across every shipped file.
"""

import io
import json
import os
import pathlib
import sys

import pytest

from memceiling.cli import (
    EXIT_FOR_VERDICT,
    EXIT_INVALID,
    EXIT_OK,
    EXIT_UNKNOWN,
    EXIT_UNSUPPORTED,
    build_parser,
    main,
)
from memceiling.schema import REASON_CODES, VERDICTS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    ".py",
    ".c",
    ".md",
    ".toml",
    ".yml",
    ".yaml",
    ".cff",
    ".txt",
    ".cfg",
    ".json",
    "",
}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "build", "dist", ".venv", ".ruff_cache"}


def shipped_text_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in ("LICENSE", ".gitignore"):
            yield path


def run_cli(argv):
    """Run main() capturing stdout, returning (exit_code, stdout_text)."""
    buffer = io.StringIO()
    saved = sys.stdout
    sys.stdout = buffer
    try:
        code = main(argv)
    finally:
        sys.stdout = saved
    return code, buffer.getvalue()


class TestExitCodes:
    def test_every_verdict_maps_to_an_exit_code(self):
        assert set(EXIT_FOR_VERDICT) == set(VERDICTS)

    def test_pass_is_zero_and_the_others_are_not(self):
        assert EXIT_FOR_VERDICT["PASS"] == EXIT_OK == 0
        assert EXIT_FOR_VERDICT["INVALID"] == EXIT_INVALID
        assert EXIT_FOR_VERDICT["UNKNOWN"] == EXIT_UNKNOWN
        assert EXIT_FOR_VERDICT["UNSUPPORTED"] == EXIT_UNSUPPORTED
        assert len({EXIT_INVALID, EXIT_UNKNOWN, EXIT_UNSUPPORTED, EXIT_OK}) == 4


class TestParser:
    def test_the_run_flags_work_without_a_subcommand(self):
        args = build_parser().parse_args(["--json", "--cpus", "0-3", "--repeats", "7"])
        assert args.json is True
        assert args.cpus == "0-3"
        assert args.repeats == 7

    def test_the_run_subcommand_accepts_the_same_flags(self):
        args = build_parser().parse_args(["run", "--cpus", "12-19"])
        assert args.command == "run"
        assert args.cpus == "12-19"

    def test_per_cluster_defaults_to_auto_and_both_overrides_exist(self):
        parser = build_parser()
        assert parser.parse_args([]).per_cluster is None
        assert parser.parse_args(["--per-cluster"]).per_cluster is True
        assert parser.parse_args(["--no-per-cluster"]).per_cluster is False

    def test_per_cluster_flags_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--per-cluster", "--no-per-cluster"])

    def test_set_governor_is_off_by_default(self):
        assert build_parser().parse_args([]).set_governor is None

    def test_code_path_defaults_to_auto_and_rejects_nonsense(self):
        assert build_parser().parse_args([]).code_path == "auto"
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--code-path", "assembly"])


class TestSchemaCommand:
    def test_json_output_lists_the_whole_vocabulary(self):
        code, text = run_cli(["schema", "--json"])
        assert code == EXIT_OK
        payload = json.loads(text)
        assert payload["schema"] == "measurement-provenance/1"
        assert set(payload["reason_codes"]) == set(REASON_CODES)
        assert payload["verdicts"] == list(VERDICTS)
        assert payload["verdict_rule"]

    def test_human_output_names_every_code(self):
        code, text = run_cli(["schema"])
        assert code == EXIT_OK
        for reason_code in REASON_CODES:
            assert reason_code in text


class TestInfoCommand:
    def test_info_measures_nothing_and_succeeds(self):
        code, text = run_cli(["info"])
        assert code == EXIT_OK
        assert "nothing was measured" in text

    def test_info_json_has_the_expected_sections(self):
        code, text = run_cli(["info", "--json"])
        assert code == EXIT_OK
        payload = json.loads(text)
        for key in ("topology", "theoretical_peak", "compiler", "unreadable"):
            assert key in payload

    def test_info_states_the_formula_whether_or_not_the_peak_is_known(self):
        _, text = run_cli(["info"])
        assert "peak_GB_s = data_rate_MT_s" in text

    def test_an_override_reaches_the_peak(self):
        _, text = run_cli(["info", "--json", "--memory-config", "4267:32:1"])
        payload = json.loads(text)
        assert payload["theoretical_peak"]["peak_gb_s"] == 17.068


class TestRunCommand:
    def test_exit_zero_forces_success_whatever_the_verdict(self, tmp_path):
        out = tmp_path / "envelope.json"
        code, text = run_cli(
            [
                "run",
                "--cpus",
                "0",
                "--code-path",
                "numpy",
                "--repeats",
                "1",
                "--bytes-per-array",
                str(2 * 1024 * 1024),
                "--llc-margin",
                "100000",
                "--min-run-seconds",
                "0.01",
                "--no-per-cluster",
                "--no-build",
                "--exit-zero",
                "--out",
                str(out),
            ]
        )
        assert code == EXIT_OK
        envelope = json.loads(out.read_text(encoding="ascii"))
        # The verdict itself is still honest in the output.
        assert envelope["verdict"] != "PASS"
        assert envelope["schema"] == "measurement-provenance/1"

    def test_without_exit_zero_a_refusal_is_a_nonzero_exit(self):
        code, _ = run_cli(
            [
                "run",
                "--cpus",
                "0",
                "--code-path",
                "numpy",
                "--repeats",
                "1",
                "--bytes-per-array",
                str(2 * 1024 * 1024),
                "--llc-margin",
                "100000",
                "--min-run-seconds",
                "0.01",
                "--no-per-cluster",
                "--no-build",
            ]
        )
        assert code in (EXIT_INVALID, EXIT_UNKNOWN)

    def test_human_output_names_the_code_path_and_the_verdict(self):
        _, text = run_cli(
            [
                "run",
                "--cpus",
                "0",
                "--code-path",
                "numpy",
                "--repeats",
                "1",
                "--bytes-per-array",
                str(2 * 1024 * 1024),
                "--min-run-seconds",
                "0.01",
                "--no-per-cluster",
                "--no-build",
                "--exit-zero",
            ]
        )
        assert "verdict:" in text
        assert "code path: numpy" in text
        assert "theoretical peak" in text


class TestRepositoryIsPureAscii:
    def test_no_shipped_file_contains_a_non_ascii_byte(self):
        offenders = []
        for path in shipped_text_files():
            data = path.read_bytes()
            for index, byte in enumerate(data):
                if byte > 0x7F:
                    offenders.append("%s byte %d" % (path.relative_to(REPO_ROOT), index))
                    break
        assert offenders == [], "non-ASCII bytes found in: %s" % offenders

    def test_no_shipped_file_contains_an_em_dash_or_en_dash(self):
        em_dash, en_dash = chr(0x2014), chr(0x2013)
        for path in shipped_text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert em_dash not in text, path
            assert en_dash not in text, path


@pytest.fixture(scope="module")
def readme():
    return (REPO_ROOT / "README.md").read_text(encoding="ascii")


class TestDocumentationCoversTheVocabulary:
    def test_the_readme_exists_and_is_substantial(self, readme):
        assert len(readme) > 4000

    def test_the_readme_documents_every_reason_code(self, readme):
        missing = [code for code in REASON_CODES if code not in readme]
        assert missing == [], "reason codes absent from README.md: %s" % missing

    def test_the_readme_documents_every_verdict(self, readme):
        for verdict in VERDICTS:
            assert verdict in readme

    def test_the_readme_states_the_theoretical_peak_formula(self, readme):
        assert "peak_GB_s = data_rate_MT_s * total_data_width_bits / 8 / 1000" in readme

    def test_the_readme_has_the_required_sections(self, readme):
        for heading in (
            "## Install",
            "## Usage",
            "## Output schema",
            "## How the theoretical peak is derived",
            "## Why per cluster",
            "## Related work",
            "## Related tools",
            "## Limitations",
        ):
            assert heading in readme, heading


class TestPackagingFilesExist:
    @pytest.mark.parametrize(
        "name",
        [
            "README.md",
            "LICENSE",
            "CITATION.cff",
            "CHANGELOG.md",
            "RELEASE_CHECKLIST.md",
            "pyproject.toml",
            "action.yml",
            os.path.join(".github", "workflows", "ci.yml"),
        ],
    )
    def test_file_is_present(self, name):
        assert (REPO_ROOT / name).is_file(), name

    def test_the_action_is_at_the_repository_root(self):
        """GitHub only lists an Action as a dependency when action.yml is at
        the repository root, so its location is a functional requirement."""
        assert (REPO_ROOT / "action.yml").is_file()

    def test_the_license_names_the_author_and_the_year(self):
        text = (REPO_ROOT / "LICENSE").read_text(encoding="ascii")
        assert "MIT" in text
        assert "Manu Nicholas Jacob" in text
        assert "2026" in text

    def test_the_citation_file_carries_the_orcid(self):
        text = (REPO_ROOT / "CITATION.cff").read_text(encoding="ascii")
        assert "0009-0007-6589-6572" in text
        # CITATION.cff splits a name into given-names and family-names.
        assert 'given-names: "Manu Nicholas"' in text
        assert 'family-names: "Jacob"' in text
        assert "Independent Researcher" in text

    def test_the_version_agrees_across_the_package_and_the_citation(self):
        from memceiling import __version__

        citation = (REPO_ROOT / "CITATION.cff").read_text(encoding="ascii")
        assert 'version: "%s"' % __version__ in citation
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="ascii")
        assert "[%s]" % __version__ in changelog
