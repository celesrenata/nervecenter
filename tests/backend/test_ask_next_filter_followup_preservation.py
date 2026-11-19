"""Preservation (follow-up round, Property 6): behavior that must not change. Passes on UNFIXED code.

Observed on unfixed code:
- a keyword that matches at least one row narrows compute() to the matching rows;
- "what is my next appointment?" computes over every upcoming row, with no "no match" text;
- with NERVE_USER_TIMEZONE unset or invalid, computed dates use the stored (UTC) date;
- bill/task computed dates are unchanged;
- "what needs my attention?" returns briefing records;
- a failing domain the question asked for still yields the "could not search" notice.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.ask import _skipped_notice
from backend.context.retrieval import retrieve
from backend.context.retrieval.aggregate import StructuredRow, compute
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.types import Record

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
HS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def cal_row(title: str, start: datetime, ref: str) -> StructuredRow:
    rec = Record(id=f"cale-{ref:0>10}", domain="calendar", title=title,
                 snippet=start.strftime("%a %b %d, %H:%M"), ts=start)
    return StructuredRow(rec, {"status": "scheduled", "ts": start, "payee": title})


offsets = st.lists(st.integers(min_value=1, max_value=2000), min_size=2, max_size=15, unique=True)


@HS
@given(offs=offsets, pick=st.data())
def test_matching_keyword_narrows_to_matching_rows(offs: list[int], pick) -> None:
    k = pick.draw(st.integers(min_value=1, max_value=len(offs)))
    rows = []
    for i, m in enumerate(offs):
        title = f"Dentist visit {i}" if i < k else f"Standup {i}"
        rows.append(cal_row(title, NOW + timedelta(hours=m), str(i)))
    res = compute(rows, parse_intent("next dentist appointment", NOW), NOW)
    assert res is not None
    expected = min(rows[:k], key=lambda r: r.fields["ts"])
    assert expected.record.title in res.text
    assert f"{k} of {len(rows)}" in res.text
    assert expected.fields["ts"].date().isoformat() in res.text


@HS
@given(offs=offsets)
def test_plain_next_appointment_uses_all_upcoming_rows(offs: list[int]) -> None:
    rows = [cal_row(f"Event {i}", NOW + timedelta(hours=m), str(i)) for i, m in enumerate(offs)]
    rows.append(cal_row("Past thing", NOW - timedelta(days=2), "past"))
    res = compute(rows, parse_intent("what is my next appointment?", NOW), NOW)
    assert res is not None
    best = min(rows[:-1], key=lambda r: r.fields["ts"])
    assert f"The next is {best.record.title} on {best.fields['ts'].date().isoformat()}" in res.text
    assert "no upcoming" not in res.text.lower() and "not matching" not in res.text.lower()
    assert f"{len(rows)} of {len(rows)}" in res.text


@pytest.mark.parametrize("tz", [None, "", "Not/AZone"])
@HS
@given(offs=offsets)
def test_unset_or_invalid_timezone_keeps_stored_dates(tz, offs: list[int], monkeypatch) -> None:
    if tz is None:
        monkeypatch.delenv("NERVE_USER_TIMEZONE", raising=False)
    else:
        monkeypatch.setenv("NERVE_USER_TIMEZONE", tz)
    rows = [cal_row(f"Event {i}", NOW + timedelta(minutes=m * 7), str(i)) for i, m in enumerate(offs)]
    res = compute(rows, parse_intent("what is my next appointment?", NOW), NOW)
    best = min(rows, key=lambda r: r.fields["ts"])
    assert res is not None
    assert best.fields["ts"].date().isoformat() in res.text
    assert res.value == best.fields["ts"].isoformat()


@HS
@given(offs=offsets)
def test_bill_next_due_dates_unchanged(offs: list[int]) -> None:
    rows = []
    for i, m in enumerate(offs):
        due = NOW + timedelta(hours=m)
        rec = Record(id=f"bill-{i:0>10}", domain="bills", title=f"Bill {i}", ts=due)
        rows.append(StructuredRow(rec, {"due_at": due, "status": "unpaid", "amount_cents": 1000 + i}))
    intent = parse_intent("when is my next bill due?", NOW)
    assert "calendar" not in intent.domain_hints and "name" not in intent.filters
    res = compute(rows, intent, NOW)
    best = min(rows, key=lambda r: r.fields["due_at"])
    assert res is not None
    assert f"The next is {best.record.title} on {best.fields['due_at'].date().isoformat()}" in res.text


def test_bill_next_question_has_no_calendar_hint_or_person_filter() -> None:
    intent = parse_intent("when is my next bill due?", NOW)
    assert intent.operator is not None and intent.operator.kind == "next"
    assert "calendar" not in intent.domain_hints
    assert not intent.filters.get("name")


def test_task_earliest_due_date_unchanged() -> None:
    rows = []
    for i, d in enumerate((5, 2, 9)):
        due = NOW + timedelta(days=d)
        rec = Record(id=f"task-{i:0>10}", domain="tasks", title=f"Task {i}", ts=due)
        rows.append(StructuredRow(rec, {"due_at": due, "status": "open"}))
    res = compute(rows, parse_intent("what is my next task due?", NOW), NOW)
    assert res is not None and "Task 1" in res.text and (NOW + timedelta(days=2)).date().isoformat() in res.text


def _briefing_service() -> SimpleNamespace:
    brief = SimpleNamespace(
        actions=[SimpleNamespace(attention_id="a1", label="Pay rent", due=NOW + timedelta(days=1))],
        waiting_on=[SimpleNamespace(request_id="w1", person="Bob", description="reply", days_elapsed=3)],
        today=[SimpleNamespace(event_id="e1", title="Standup", start=NOW)],
        overnight=SimpleNamespace(emails=1, messages=2, voicemails=0),
        generated_at=NOW,
    )
    return SimpleNamespace(compose=lambda *_a, **_k: brief)


def test_attention_question_returns_briefing_records() -> None:
    state = SimpleNamespace(briefing_service=_briefing_service())
    res = asyncio.run(retrieve(state, "what needs my attention?", NOW))
    assert "briefing" in res.intent.domain_hints
    assert "briefing" in res.searched and "briefing" not in res.skipped
    assert any(r.domain == "briefing" and "Pay rent" in r.title for r in res.records)


@HS
@given(domain=st.sampled_from(["bills", "mail", "tasks"]))
def test_requested_domain_failure_still_yields_notice(domain: str) -> None:
    async def boom(state, intent, limit):
        raise RuntimeError("down")

    question = {"bills": "what bills do i have?", "mail": "any mail from bob?", "tasks": "what tasks are open?"}[domain]
    res = asyncio.run(retrieve(SimpleNamespace(), question, NOW, adapters={domain: boom}))
    assert domain in res.skipped
    assert f"I could not search {domain}" in _skipped_notice(res)
