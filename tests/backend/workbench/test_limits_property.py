"""Property 12: Message and selection limits.

**Validates: Requirements 3.8, 3.9, 4.10, 4.11**
"""
from hypothesis import given, settings, strategies as st

from backend.workbench.validation import (
    INSTRUCTION_MAX,
    MESSAGE_MAX,
    SELECTION_MAX,
    validate_message,
    validate_selection_action,
)

_lengths = lambda cap: st.one_of(  # noqa: E731
    st.integers(0, cap + 50),
    st.sampled_from([0, 1, 2, cap - 1, cap, cap + 1]),
)


@settings(max_examples=200, deadline=None)
@given(n=_lengths(MESSAGE_MAX), running=st.booleans())
def test_message_accepted_iff_length_and_running(n, running):
    expected = 1 <= n <= 20000 and running
    assert validate_message("a" * n, running) is expected


@settings(max_examples=200, deadline=None)
@given(
    sel=_lengths(SELECTION_MAX),
    ins=_lengths(INSTRUCTION_MAX),
    active=st.booleans(),
)
def test_selection_action_accepted_iff_limits_and_active(sel, ins, active):
    expected = sel <= 100000 and 1 <= ins <= 10000 and active
    assert validate_selection_action("s" * sel, "i" * ins, active) is expected
