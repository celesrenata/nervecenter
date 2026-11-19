"""Bug condition exploration (defect B): computed dates use UTC, not the user timezone (EXPECTED to fail pre-fix).

Findings (investigation B1-B4, UNFIXED code):
- B1: deployments/kubernetes/base/api-deployment.yaml sets NERVE_USER_TIMEZONE=America/Los_Angeles (bc450082).
  2026-11-18 18:30 PST == 2026-11-19 02:30 UTC, matching the production "2026-11-19".
- B2: service.retrieve passes the raw UTC `moment` to _compute/compute; only parse_intent gets
  _user_now(moment). Row timestamps are UTC-aware. aggregate._dt only retags naive values, so aware UTC
  values stay UTC.
- B3: aggregate.py date formatting: `when.date().isoformat()` (next/earliest/latest answer, line ~306),
  `d.date().isoformat()` (line ~273, top_n), `due.date().isoformat()` (line ~289), and `_iso(when)` for
  the Computation value (line ~306); `_iso` also formats scope window bounds.
- B4: communications._when_text(row, tz) formats the snippet in the user zone (tz = intent.now.tzinfo).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval.aggregate import StructuredRow, compute
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.service import _user_now
from backend.context.retrieval.sources.communications import _when_text
from backend.context.retrieval.types import Record

QUESTION = "what is my next appointment?"
ZONES = ["America/Los_Angeles", "America/New_York", "Asia/Tokyo", "Australia/Sydney", "Europe/Berlin"]


class _Ev:
    def __init__(self, start: datetime, end: datetime) -> None:
        self.start, self.end, self.all_day = start, end, False


def _row(start: datetime, snippet: str) -> StructuredRow:
    rec = Record(id="cale-0000000001", domain="calendar", title="Family Video Call", snippet=snippet, ts=start)
    return StructuredRow(rec, {"status": "scheduled", "ts": start, "payee": "Family Video Call"})


def _run(start: datetime, zone: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NERVE_USER_TIMEZONE", zone)
    moment = start - timedelta(days=1)  # UTC moment, as service.retrieve passes it
    intent = parse_intent(QUESTION, _user_now(moment))
    tz = ZoneInfo(zone)
    snippet = _when_text(_Ev(start, start + timedelta(minutes=45)), tz)
    result = compute([_row(start, snippet)], intent, moment)
    return result, start.astimezone(tz), snippet


def test_production_example_uses_user_date(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 11, 19, 2, 30, tzinfo=UTC)  # Wed Nov 18 18:30 Pacific
    result, local, snippet = _run(start, "America/Los_Angeles", monkeypatch)
    assert "Nov 18, 18:30" in snippet
    assert result is not None
    assert "2026-11-18" in result.text and "2026-11-19" not in result.text
    assert str(result.value).startswith("2026-11-18")


@settings(max_examples=60, deadline=None)
@given(
    start=st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2027, 12, 31)).map(lambda d: d.replace(tzinfo=UTC)),
    zone=st.sampled_from(ZONES),
)
def test_computed_date_matches_user_zone(start: datetime, zone: str) -> None:
    local = start.astimezone(ZoneInfo(zone))
    if local.date() == start.date():
        return  # only the bug condition: UTC date differs from the user-zone date
    mp = pytest.MonkeyPatch()
    try:
        result, local, snippet = _run(start, zone, mp)
    finally:
        mp.undo()
    assert result is not None
    assert local.date().isoformat() in result.text
    assert str(result.value).startswith(local.date().isoformat())
