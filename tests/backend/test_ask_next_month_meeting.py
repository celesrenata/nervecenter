"""Bug condition exploration: next-month / "next see pam" calendar lookups (EXPECTED to fail pre-fix)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval import retrieve
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.ranker import rank, score_record
from backend.context.retrieval.sources.communications import calendar
from backend.context.retrieval.types import Record

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
JULY = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def rec(title: str, snippet: str = "", ts: datetime = JULY, ref: str = "1") -> Record:
    return Record(id=f"cale-{ref:0>10}", domain="calendar", title=title, snippet=snippet, ts=ts)


def event(title: str, start: datetime, *, all_day: bool = False, attendees=None, names=None, uid=None):
    extra = {}
    if attendees:
        extra["attendees"] = attendees
    if names:
        extra["attendee_names"] = names
    return SimpleNamespace(
        account_id="acct", ical_uid=uid or f"uid-{title}", title=title, start=start,
        end=start + timedelta(hours=1), all_day=all_day, location=None, description=None,
        deleted=False, status="confirmed", extra=extra,
    )


class _Calendar:
    def __init__(self, rows):
        self._rows = rows

    async def list_events(self, *, start=None, end=None, limit=500, **_):
        return [r for r in self._rows if start <= r.start < end][:limit]


def state_with(rows):
    return SimpleNamespace(repository_registry=SimpleNamespace(calendar=_Calendar(rows)))


# -- Variant A: windowed gate ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "record"),
    [
        ("do I have anything planned next month?", rec("Standup")),
        ("when is my meeting with Bob next month?", rec("Quarterly review", "Wed Jul 15, 10:00-11:00")),
        ("what's my dentist appointment next month?", rec("Dr. Lee 3pm")),
    ],
)
def test_in_window_calendar_event_survives_term_gating(question: str, record: Record) -> None:
    intent = parse_intent(question, NOW)
    assert intent.window is not None and "calendar" in intent.domain_hints and intent.has_terms
    assert score_record(record, intent, NOW) > 0.0
    assert record.id in {r.id for r in rank([record], intent, NOW)}


def test_zero_hit_candidate_ranks_below_a_term_hit() -> None:
    intent = parse_intent("do I have anything planned next month?", NOW)
    hit = rec("Planned offsite", ref="2")
    miss = rec("Standup", ref="3")
    assert score_record(hit, intent, NOW) > score_record(miss, intent, NOW) > 0.0


# -- Adapter: attendees in snippet, recurring, all-day -------------------------------------------
def test_adapter_snippet_has_attendees_and_handles_recurring_and_all_day() -> None:
    rows = [
        event("Quarterly review", JULY, attendees=["bob@example.com"], names=["Bob Smith"]),
        event("Weekly sync", datetime(2026, 7, 8, 9, 0, tzinfo=UTC), uid="rec_20260708"),
        event("Weekly sync", datetime(2026, 7, 22, 9, 0, tzinfo=UTC), uid="rec_20260722"),
        event("Holiday", datetime(2026, 7, 31, 0, 0, tzinfo=UTC), all_day=True),
    ]
    intent = parse_intent("what meetings do I have next month?", NOW)
    out = asyncio.run(calendar(state_with(rows), intent, 50))
    assert out is not None
    by_title = {}
    for r in out:
        by_title.setdefault(r.title, []).append(r)
    assert len(by_title["Weekly sync"]) == 2  # recurring instances present
    assert "(all day)" in by_title["Holiday"][0].snippet and "Jul 31" in by_title["Holiday"][0].snippet
    snip = by_title["Quarterly review"][0].snippet
    assert "bob@example.com" in snip and "Bob Smith" in snip


# -- Variant B: next operator --------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    ["when do i next see pam?", "when do i next meet pam?", "when is my next meeting with pam?",
     "when is my next appointment with pam?", "when do i next visit pam?"],
)
def test_calendar_verbs_hint_calendar_domain(question: str) -> None:
    intent = parse_intent(question, NOW)
    assert intent.operator is not None and intent.operator.kind == "next"
    assert "calendar" in intent.domain_hints


def _next_pam(rows):
    return asyncio.run(retrieve(state_with(rows), "when do i next see pam?", NOW))


def test_next_see_pam_attendee_only_event_is_cited() -> None:
    soon = NOW + timedelta(days=3)
    rows = [event("Standup", NOW + timedelta(days=1)),
            event("Quarterly review", soon, attendees=["pam@example.com"], names=["Pam Beesly"])]
    result = _next_pam(rows)
    assert [r.title for r in result.records] == ["Quarterly review"] or \
        (result.records and result.records[0].title == "Quarterly review")


def test_next_see_pam_title_event_is_cited() -> None:
    rows = [event("Standup", NOW + timedelta(days=1)), event("Lunch with Pam", NOW + timedelta(days=5))]
    result = _next_pam(rows)
    assert result.records and result.records[0].title == "Lunch with Pam"


def test_next_see_pam_falls_back_to_upcoming_candidates() -> None:
    rows = [event("Standup", NOW + timedelta(days=1)), event("Dentist", NOW + timedelta(days=5))]
    result = _next_pam(rows)
    assert {r.title for r in result.records} >= {"Standup", "Dentist"}
