"""Tests for the pinning logic.

These assert the LOGIC of reporting the effective mask rather than the
requested one, which is the property that matters, and they pass on a
platform with no pinning at all.
"""

import os

import pytest

from membw.affinity import (
    AffinityResult,
    _cpus_from_mask,
    _mask_from_cpus,
    current_cpu,
    pin_current_thread,
    pinning_supported,
)


class TestMaskArithmetic:
    @pytest.mark.parametrize(
        "cpus,mask",
        [([0], 0b1), ([1], 0b10), ([0, 1], 0b11), ([0, 3], 0b1001), ([], 0)],
    )
    def test_mask_from_cpus(self, cpus, mask):
        assert _mask_from_cpus(cpus) == mask

    def test_roundtrip(self):
        for cpus in ([0], [0, 1, 2, 3], [12, 13, 14, 15], [0, 19]):
            assert list(_cpus_from_mask(_mask_from_cpus(cpus), 64)) == sorted(cpus)


class TestAsRequestedLogic:
    """The core contract: as_requested is about the EFFECTIVE mask."""

    def test_matching_effective_mask_is_as_requested(self):
        result = AffinityResult(
            requested=(0, 1), effective=(0, 1), supported=True, mechanism="test"
        )
        assert result.as_requested is True

    def test_a_narrowed_effective_mask_is_not_as_requested(self):
        result = AffinityResult(
            requested=(0, 1, 2, 3), effective=(0, 1), supported=True, mechanism="test"
        )
        assert result.as_requested is False

    def test_a_widened_effective_mask_is_not_as_requested(self):
        result = AffinityResult(
            requested=(0,), effective=(0, 1), supported=True, mechanism="test"
        )
        assert result.as_requested is False

    def test_an_unreadable_effective_mask_is_never_as_requested(self):
        result = AffinityResult(
            requested=(0, 1), effective=None, supported=True, mechanism="test"
        )
        assert result.as_requested is False

    def test_unsupported_pinning_is_never_as_requested_even_if_masks_match(self):
        result = AffinityResult(
            requested=(0, 1), effective=(0, 1), supported=False, mechanism="none"
        )
        assert result.as_requested is False

    def test_order_does_not_matter(self):
        result = AffinityResult(
            requested=(3, 1, 2), effective=(1, 2, 3), supported=True, mechanism="test"
        )
        assert result.as_requested is True

    def test_as_dict_reports_both_masks_separately(self):
        result = AffinityResult(
            requested=(0, 1, 2, 3), effective=(0, 1), supported=True, mechanism="test"
        )
        payload = result.as_dict()
        assert payload["requested_cpu_list"] == "0-3"
        assert payload["effective_cpu_list"] == "0-1"
        assert payload["mask_as_requested"] is False
        assert payload["mechanism"] == "test"


class TestPinning:
    def test_an_empty_request_is_refused_not_silently_accepted(self):
        result = pin_current_thread([])
        assert result.as_requested is False
        assert result.error

    @pytest.mark.skipif(not pinning_supported(), reason="no pinning on this platform")
    def test_pinning_to_cpu_zero_takes_effect(self):
        original = (
            set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        )
        try:
            result = pin_current_thread([0])
            assert result.supported is True
            assert result.effective is not None
            assert result.as_requested is True
            assert result.effective == (0,)
            observed = current_cpu()
            if observed is not None:
                assert observed == 0
        finally:
            if original:
                os.sched_setaffinity(0, original)

    @pytest.mark.skipif(not pinning_supported(), reason="no pinning on this platform")
    def test_the_mechanism_is_named_in_the_record(self):
        result = pin_current_thread([0])
        assert result.mechanism
        assert "affinity" in result.mechanism.lower()

    @pytest.mark.skipif(
        not pinning_supported() or (os.cpu_count() or 1) < 2,
        reason="needs pinning and at least two CPUs",
    )
    def test_repinning_reports_the_new_mask_not_the_old_one(self):
        original = (
            set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        )
        try:
            pin_current_thread([0])
            second = pin_current_thread([1])
            assert second.effective == (1,)
            assert second.as_requested is True
        finally:
            if original:
                os.sched_setaffinity(0, original)


class TestCurrentCpu:
    def test_returns_an_int_or_none_and_never_raises(self):
        value = current_cpu()
        assert value is None or (isinstance(value, int) and value >= 0)
