"""Tests for the output envelope.

The outer envelope is shared with sibling tools, so its key set, key order
and value types are a contract. These tests pin that contract down. They do
not assert any bandwidth value.
"""

import datetime
import json
import re

import pytest

from membw import __version__
from membw.run import _build_envelope, _utc_now_iso
from membw.schema import SCHEMA_ID, SEVERITIES, TOOL_NAME, VERDICTS

ENVELOPE_KEYS = [
    "schema",
    "tool",
    "taken_at_utc",
    "duration_s",
    "platform",
    "verdict",
    "reasons",
    "unreadable",
    "measurement",
]


@pytest.fixture
def envelope():
    return _build_envelope(
        started_at="2026-11-01T00:00:00Z",
        duration_s=1.2345678,
        verdict="PASS",
        reasons=[
            {
                "code": "WORKING_SET_BELOW_LLC",
                "severity": "invalidating",
                "detail": "An example detail sentence.",
            }
        ],
        unreadable=["/proc/meminfo"],
        measurement={"achieved": {"ceiling_gb_s": 53.9}},
    )


class TestEnvelopeShape:
    def test_has_exactly_the_agreed_top_level_keys(self, envelope):
        assert list(envelope) == ENVELOPE_KEYS

    def test_schema_identifier_is_fixed(self, envelope):
        assert envelope["schema"] == SCHEMA_ID == "measurement-provenance/1"

    def test_tool_block(self, envelope):
        assert envelope["tool"] == {"name": TOOL_NAME, "version": __version__}
        assert envelope["tool"]["name"] == "membw"

    def test_platform_block_has_the_four_agreed_fields(self, envelope):
        assert set(envelope["platform"]) == {"system", "release", "machine", "python"}
        for value in envelope["platform"].values():
            assert isinstance(value, str)

    def test_verdict_is_from_the_vocabulary(self, envelope):
        assert envelope["verdict"] in VERDICTS

    def test_duration_is_a_rounded_float(self, envelope):
        assert isinstance(envelope["duration_s"], float)
        assert envelope["duration_s"] == 1.2346

    def test_reasons_entries_have_exactly_three_fields(self, envelope):
        for item in envelope["reasons"]:
            assert set(item) == {"code", "severity", "detail"}
            assert item["severity"] in SEVERITIES

    def test_unreadable_is_a_list_of_strings(self, envelope):
        assert isinstance(envelope["unreadable"], list)
        assert all(isinstance(item, str) for item in envelope["unreadable"])

    def test_everything_tool_specific_lives_under_measurement(self, envelope):
        assert "achieved" in envelope["measurement"]
        # None of membw's own payload may leak to the top level.
        for key in ("achieved", "topology", "theoretical_peak", "code_path"):
            assert key not in envelope

    def test_the_whole_envelope_is_json_serialisable(self, envelope):
        text = json.dumps(envelope)
        assert json.loads(text) == envelope

    def test_the_serialised_envelope_is_pure_ascii(self, envelope):
        text = json.dumps(envelope)
        text.encode("ascii")


class TestTimestamp:
    def test_is_iso8601_utc_with_a_z_suffix(self):
        stamp = _utc_now_iso()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), stamp

    def test_parses_back_as_utc(self):
        stamp = _utc_now_iso()
        parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.timedelta(0)

    def test_carries_no_microseconds(self):
        assert "." not in _utc_now_iso()


class TestUnreadableDeduplication:
    def test_duplicate_and_none_entries_are_dropped_in_order(self):
        from membw.run import _dedupe

        assert _dedupe(["a", "b", "a", None, "c", "b"]) == ["a", "b", "c"]

    def test_empty(self):
        from membw.run import _dedupe

        assert _dedupe([]) == []
