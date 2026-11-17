"""Ask retrieval regressions: relative windows, calendar and bills adapters. Fakes only."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.bills.service import BillService
from backend.context.retrieval import retrieve
from backend.context.retrieval.intent import parse_intent

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)  # a Wednesday
TODAY = NOW.replace(hour=0)


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        ("How many calendar events do I have in the next 7 days?", TODAY, TODAY + timedelta(days=7)),
        ("what is on my calendar over the next 2 weeks", TODAY, TODAY + timedelta(days=14)),
        ("anything today?", TODAY, TODAY + timedelta(days=1)),
        ("what is on this week", TODAY - timedelta(days=2), TODAY + timedelta(days=5)),
        ("meetings in the past 3 days", TODAY - timedelta(days=3), TODAY + timedelta(days=1)),
    ],
)
def test_relative_phrases_set_a_window(question: str, start: datetime, end: datetime) -> None:
    intent = parse_intent(question, NOW)
    assert intent.window == (start, end)


def test_count_words_are_not_search_keywords() -> None:
    intent = parse_intent("How many calendar events do I have in the next 7 days?", NOW)
    assert intent.keywords == ()
    assert parse_intent("What bills do I have coming due?", NOW).keywords == ()


def _event(title: str, days: float, *, deleted: bool = False, status: str = "confirmed") -> SimpleNamespace:
    start = NOW + timedelta(days=days)
    return SimpleNamespace(
        account_id="acct", ical_uid=f"uid-{title}", title=title, start=start,
        end=start + timedelta(hours=1), all_day=False, location="Office", description=None,
        deleted=deleted, status=status,
    )


class _Calendar:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def list_events(self, *, start=None, end=None, limit=500, **_: object):
        return [r for r in self._rows if start <= r.start < end][:limit]


async def test_calendar_adapter_returns_upcoming_events_in_the_window() -> None:
    rows = [
        _event("Standup", 1), _event("Dentist", 3), _event("Offsite", 20),
        _event("Gone", 2, deleted=True), _event("Called off", 2, status="cancelled"),
        _event("Last month", -30),
    ]
    state = SimpleNamespace(repository_registry=SimpleNamespace(calendar=_Calendar(rows)))
    result = await retrieve(state, "How many calendar events do I have in the next 7 days?", NOW)
    titles = sorted(r.title for r in result.records if r.domain == "calendar")
    assert titles == ["Dentist", "Standup"]
    first = next(r for r in result.records if r.title == "Standup")
    assert "Thu Jun 04, 12:00-13:00" in first.snippet
    assert "Office" in first.snippet


async def test_calendar_without_a_window_lists_future_events() -> None:
    rows = [_event("Later", 40), _event("Soon", 2)]
    state = SimpleNamespace(repository_registry=SimpleNamespace(calendar=_Calendar(rows)))
    result = await retrieve(state, "What is my next appointment on the calendar?", NOW)
    assert {r.title for r in result.records if r.domain == "calendar"} == {"Soon", "Later"}


async def test_bills_adapter_returns_bills_and_proposed_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    due = (NOW + timedelta(days=5)).isoformat()

    async def list_bills(self, *, status=None):
        return [{"id": "b1", "payee": "Water Co", "amount_cents": "4250", "currency": "USD",
                 "status": "pending", "due_at": due}]

    async def list_candidates(self, *, status=None):
        return [
            {"id": "c1", "created_at": NOW.isoformat(), "duplicate_of": None,
             "extracted_fields": {"payee": {"value": "Power Co", "confidence": 0.9},
                                  "amount_cents": {"value": "9000", "confidence": 0.8}}},
            {"id": "c2", "created_at": NOW.isoformat(), "duplicate_of": "c1",
             "extracted_fields": {"payee": {"value": "Power Co dup", "confidence": 0.9}}},
        ]

    monkeypatch.setattr(BillService, "list_bills", list_bills)
    monkeypatch.setattr(BillService, "list_candidates", list_candidates)
    state = SimpleNamespace(repository_registry=SimpleNamespace(session_factory=object()))
    result = await retrieve(state, "What bills do I have coming due?", NOW)
    titles = sorted(r.title for r in result.records if r.domain == "bills")
    assert titles == ["Bill: Water Co", "Possible bill: Power Co"]
    assert "42.50 USD" in next(r for r in result.records if r.title == "Bill: Water Co").snippet
