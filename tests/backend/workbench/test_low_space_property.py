"""Property 24: Low-space warning threshold.

Validates: Requirements 9.5
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from backend.workbench.validation import low_space

THRESHOLD = 5_368_709_120

free_bytes = st.one_of(
    st.integers(min_value=0, max_value=THRESHOLD * 4),
    st.integers(min_value=THRESHOLD - 1000, max_value=THRESHOLD + 1000),
    st.sampled_from([0, THRESHOLD - 1, THRESHOLD, THRESHOLD + 1]),
)


@settings(max_examples=200)
@given(free_bytes)
def test_low_space_iff_below_threshold(value: int) -> None:
    assert low_space(value) is (value < THRESHOLD)


def test_low_space_boundaries() -> None:
    assert low_space(THRESHOLD - 1) is True
    assert low_space(THRESHOLD) is False
