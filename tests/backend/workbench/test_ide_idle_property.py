"""Property 16: Idle reaper decision.

Validates: Requirements 5.9
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings, strategies as st

from backend.workbench.ide import IdeInstance, select_idle

_LIMIT = timedelta(minutes=60)
_now = st.datetimes(
    min_value=datetime(2001, 1, 1), max_value=datetime(2090, 1, 1), timezones=st.just(UTC)
)
_idle_secs = st.integers(min_value=-7200, max_value=30 * 86400)


def _inst(i: int, last: datetime) -> IdeInstance:
    return IdeInstance(
        slug=f"p{i}", kind="code", pod_name=f"pod{i}", pod_ip=None, last_traffic_at=last
    )


@settings(max_examples=100)
@given(now=_now, idles=st.lists(_idle_secs, max_size=12), naive=st.booleans())
def test_selected_iff_idle_at_least_60_minutes(now, idles, naive):
    instances = [_inst(i, now - timedelta(seconds=s)) for i, s in enumerate(idles)]
    if naive:
        instances = [_inst(i, x.last_traffic_at.replace(tzinfo=None)) for i, x in enumerate(instances)]
        now_arg = now.replace(tzinfo=None)
    else:
        now_arg = now
    got = {i.slug for i in select_idle(instances, now_arg)}
    want = {f"p{i}" for i, s in enumerate(idles) if timedelta(seconds=s) >= _LIMIT}
    assert got == want


@settings(max_examples=100)
@given(now=_now, secs=st.integers(min_value=-7200, max_value=3599))
def test_recently_active_never_selected(now, secs):
    inst = _inst(0, now - timedelta(seconds=secs))
    assert select_idle([inst], now) == []


@settings(max_examples=50)
@given(now=_now, extra=st.integers(min_value=0, max_value=10**6))
def test_boundary_included_and_just_under_excluded(now, extra):
    at = _inst(0, now - _LIMIT - timedelta(seconds=extra))
    assert select_idle([at], now) == [at]
    just_under = _inst(1, now - timedelta(minutes=59, seconds=59))
    assert select_idle([just_under], now) == []


def test_naive_datetimes_treated_as_utc():
    now = datetime(2025, 1, 1, 12, 0, 0)
    aware_last = datetime(2025, 1, 1, 11, 0, 0, tzinfo=UTC)
    inst = _inst(0, aware_last)
    assert select_idle([inst], now) == [inst]
    assert select_idle([inst], now.replace(tzinfo=UTC)) == [inst]
    naive_last = _inst(1, datetime(2025, 1, 1, 11, 0, 1))
    assert select_idle([naive_last], now.replace(tzinfo=UTC)) == []
