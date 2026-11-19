"""Preservation: Ask behaviour that must not change when the next-month meeting fix lands.

Property 2 of spec ask-next-month-meeting. Observed on the UNFIXED code (NOW = Wed 2026-06-03
12:00 UTC); these tests PASS before the fix and must keep passing after it.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval import retrieve
from backend.context.retrieval.intent import AGGREGATE, DOMAIN_LIST, LOOKUP, OVERVIEW, Intent, _stem, parse_intent
from backend.context.retrieval.ranker import score_record
from backend.context.retrieval.sources.communications import calendar
from backend.context.retrieval.types import Record
from test_ask_next_month_meeting import NOW, event, state_with

PROFILE = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
UTC_ = UTC


# -- snapshot reference scorer: copy of the current (unfixed) score_record -------------------------
def _ref_aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _ref_words(text: str) -> set[str]:
    return {_stem(t) for t in re.findall(r"[a-z0-9][a-z0-9_\-']*", text.lower())}


def ref_score(record: Record, intent: Intent, now: datetime) -> float:
    hay = f"{record.title} {record.snippet}".lower()
    words = _ref_words(hay)
    score = 0.0
    hits = 0
    for kw in intent.keywords:
        if kw in words or (len(kw) > 4 and kw in hay):
            score += 1.0
            hits += 1
    for name in intent.names:
        if name in words:
            score += 3.0
            hits += 1
    for phrase in intent.phrases:
        if phrase in hay:
            score += 3.0
            hits += 1
    in_window = False
    ts = _ref_aware(record.ts)
    if intent.window and ts is not None:
        start, end = _ref_aware(intent.window[0]), _ref_aware(intent.window[1])
        in_window = start <= ts < end
        if in_window:
            score += 2.0
    selected_by_domain = intent.kind in (AGGREGATE, DOMAIN_LIST, OVERVIEW)
    if intent.has_terms and not selected_by_domain:
        if hits == 0:
            return 0.0
    elif selected_by_domain and hits > 0:
        pass
    elif intent.window is not None:
        if not in_window and not (selected_by_domain and record.domain in intent.domain_hints):
            return 0.0
        if not in_window:
            score = 0.5
    elif record.domain in intent.domain_hints:
        score = 0.5
    else:
        return 0.0
    if record.domain in intent.domain_hints:
        score += 1.0
    if ts is not None:
        age_days = abs((_ref_aware(now) - ts).total_seconds()) / 86400
        score += max(0.0, 1.0 - min(age_days, 365.0) / 365.0) * 0.5
    return round(score, 4)


def is_bug_condition(intent: Intent, record: Record) -> bool:
    ts = _ref_aware(record.ts)
    in_window = bool(intent.window and ts is not None
                     and _ref_aware(intent.window[0]) <= ts < _ref_aware(intent.window[1]))
    if not (intent.window and "calendar" in intent.domain_hints and intent.has_terms
            and intent.kind not in (AGGREGATE, DOMAIN_LIST, OVERVIEW)
            and record.domain == "calendar" and in_window):
        return False
    hay = f"{record.title} {record.snippet}".lower()
    words = _ref_words(hay)
    hits = (sum(1 for k in intent.keywords if k in words or (len(k) > 4 and k in hay))
            + sum(1 for n in intent.names if n in words)
            + sum(1 for p in intent.phrases if p in hay))
    return hits == 0


# -- strategies --------------------------------------------------------------------------------
DOMAINS = ["calendar", "mail", "messages", "bills", "tasks", "wellness", "voicemail"]
WORDS = ["planned", "dentist", "standup", "review", "invoice", "lunch", "budget", "garden"]
NAMES = ["bob", "pam", "laura"]
BASE = datetime(2026, 7, 1, tzinfo=UTC)


@st.composite
def intents(draw) -> Intent:
    window = None
    if draw(st.booleans()):
        start = BASE + timedelta(days=draw(st.integers(-40, 40)))
        window = (start, start + timedelta(days=draw(st.integers(1, 31))))
    return Intent(
        question="generated",
        keywords=tuple(draw(st.lists(st.sampled_from(WORDS), max_size=3, unique=True))),
        names=tuple(draw(st.lists(st.sampled_from(NAMES), max_size=2, unique=True))),
        window=window,
        domain_hints=frozenset(draw(st.lists(st.sampled_from(DOMAINS), max_size=3))),
        kind=draw(st.sampled_from([LOOKUP, AGGREGATE, DOMAIN_LIST, OVERVIEW])),
        now=NOW,
    )


@st.composite
def records(draw) -> Record:
    ts = None if draw(st.integers(0, 9)) == 0 else BASE + timedelta(days=draw(st.integers(-60, 90)),
                                                                     hours=draw(st.integers(0, 23)))
    title = " ".join(draw(st.lists(st.sampled_from(WORDS + NAMES), min_size=1, max_size=3)))
    snippet = " ".join(draw(st.lists(st.sampled_from(WORDS + NAMES + ["meeting"]), max_size=4)))
    return Record(id=f"rec-{draw(st.integers(0, 10**9)):010x}", domain=draw(st.sampled_from(DOMAINS)),
                  title=title, snippet=snippet, ts=ts)


# -- Property: non-bug inputs score identically -----------------------------------------------------
@PROFILE
@given(intent=intents(), record=records())
def test_non_bug_inputs_score_like_the_snapshot(intent: Intent, record: Record) -> None:
    """**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**"""
    if is_bug_condition(intent, record):
        return
    assert score_record(record, intent, NOW) == ref_score(record, intent, NOW)


def _in_window_calendar(intent_q: str, title: str, snippet: str = "") -> tuple[Intent, Record]:
    intent = parse_intent(intent_q, NOW)
    return intent, Record(id="cale-0000000001", domain="calendar", title=title, snippet=snippet,
                          ts=datetime(2026, 7, 15, 10, tzinfo=UTC))


# -- Observed scores ----------------------------------------------------------------------------
def test_non_calendar_records_with_window_and_no_hits_score_zero() -> None:
    """**Validates: Requirements 3.5**"""
    intent = parse_intent("do I have anything planned next month?", NOW)
    for domain in ("bills", "mail", "tasks"):
        rec = Record(id=f"{domain[:4]}-0000000001", domain=domain, title="Standup", snippet="",
                     ts=datetime(2026, 7, 15, 10, tzinfo=UTC))
        assert score_record(rec, intent, NOW) == 0.0


def test_out_of_window_calendar_with_unmatched_terms_scores_zero() -> None:
    """**Validates: Requirements 3.6**"""
    intent = parse_intent("do I have anything planned next month?", NOW)
    rec = Record(id="cale-0000000002", domain="calendar", title="Standup", snippet="",
                 ts=datetime(2026, 8, 15, 10, tzinfo=UTC))
    assert score_record(rec, intent, NOW) == 0.0


def test_calendar_term_hit_scores_unchanged() -> None:
    """**Validates: Requirements 3.1**"""
    intent, rec = _in_window_calendar("do I have anything planned next month?", "Planned offsite")
    assert score_record(rec, intent, NOW) == ref_score(rec, intent, NOW) > 3.0


# -- Window parsing and operator parsing ----------------------------------------------------------
def _d(m: int, d: int) -> datetime:
    return datetime(2026, m, d, tzinfo=UTC)


def test_windows_parse_as_before() -> None:
    """**Validates: Requirements 3.2**"""
    expected = {
        "this week": (_d(6, 1), _d(6, 8)), "next week": (_d(6, 8), _d(6, 15)),
        "last month": (_d(5, 1), _d(6, 1)), "this month": (_d(6, 1), _d(7, 1)),
        "tomorrow": (_d(6, 4), _d(6, 5)), "today": (_d(6, 3), _d(6, 4)),
        "what meetings do I have next month?": (_d(7, 1), _d(8, 1)),
    }
    for q, window in expected.items():
        assert parse_intent(q, NOW).window == window, q


def test_next_appointment_has_next_operator_and_no_window() -> None:
    """**Validates: Requirements 3.3**"""
    intent = parse_intent("what is my next appointment?", NOW)
    assert intent.operator is not None and intent.operator.kind == "next"
    assert intent.window is None and "calendar" in intent.domain_hints and intent.kind == AGGREGATE


def test_no_term_next_month_question_returns_in_window_events() -> None:
    """**Validates: Requirements 3.4**"""
    rows = [event("A", datetime(2026, 7, 8, 9, tzinfo=UTC)), event("B", datetime(2026, 7, 9, 9, tzinfo=UTC)),
            event("Out", datetime(2026, 8, 9, 9, tzinfo=UTC)), event("Before", datetime(2026, 6, 20, 9, tzinfo=UTC))]
    result = asyncio.run(retrieve(state_with(rows), "what meetings do I have next month?", NOW))
    assert [r.title for r in result.records] == ["A", "B"]


def test_empty_month_yields_no_records() -> None:
    """**Validates: Requirements 3.5**"""
    rows = [event("June thing", datetime(2026, 6, 20, 9, tzinfo=UTC))]
    result = asyncio.run(retrieve(state_with(rows), "what meetings do I have next month?", NOW))
    assert result.records == []


# -- Adapter: exclusions and default ranges ----------------------------------------------------------
def test_cancelled_and_deleted_events_are_excluded() -> None:
    """**Validates: Requirements 3.7**"""
    keep = event("Keep", datetime(2026, 7, 8, 9, tzinfo=UTC))
    cancelled = event("Cancelled", datetime(2026, 7, 9, 9, tzinfo=UTC))
    cancelled.status = "cancelled"
    deleted = event("Deleted", datetime(2026, 7, 10, 9, tzinfo=UTC))
    deleted.deleted = True
    out = asyncio.run(calendar(state_with([keep, cancelled, deleted]),
                               parse_intent("what meetings do I have next month?", NOW), 50))
    assert [r.title for r in out] == ["Keep"]


def test_no_window_calendar_range_is_30_days_back_to_180_ahead() -> None:
    """**Validates: Requirements 3.8**"""
    seen: dict[str, datetime] = {}

    class Spy:
        async def list_events(self, *, start=None, end=None, limit=500, **_):
            seen.update(start=start, end=end)
            return []

    from types import SimpleNamespace
    state = SimpleNamespace(repository_registry=SimpleNamespace(calendar=Spy()))
    intent = parse_intent("what is on my calendar", NOW)
    assert intent.window is None
    asyncio.run(calendar(state, intent, 50))
    assert seen == {"start": NOW - timedelta(days=30), "end": NOW + timedelta(days=180)}


def test_count_question_counts_typed_calendar_rows() -> None:
    """**Validates: Requirements 3.9**"""
    rows = [event(t, datetime(2026, 7, d, 9, tzinfo=UTC)) for t, d in (("A", 8), ("B", 9), ("C", 10))]
    result = asyncio.run(retrieve(state_with(rows), "how many meetings do I have next month?", NOW))
    assert result.computation is not None
    assert result.computation.kind == "count" and result.computation.value == 3


# -- next operator outside calendar; capitalised names -------------------------------------------------
def test_next_bill_parses_with_no_calendar_hint_and_no_person_filter() -> None:
    """**Validates: Requirements 3.3, 3.9**"""
    intent = parse_intent("when is my next bill due?", NOW)
    assert intent.operator is not None and intent.operator.kind == "next"
    assert intent.kind == AGGREGATE and intent.window is None
    assert intent.domain_hints == frozenset({"bills"})
    assert intent.filters == {"status": "unpaid"}
    assert intent.keywords == () and intent.names == ()


def test_capitalised_names_parse_as_names() -> None:
    """**Validates: Requirements 3.9**"""
    pam = parse_intent("when do I next see Pam?", NOW)
    assert pam.names == ("pam",) and pam.filters == {"name": "pam"} and pam.keywords == ()
    bob = parse_intent("what is my meeting with Bob?", NOW)
    assert bob.names == ("bob",) and bob.filters == {"name": "bob"}
    assert "calendar" in bob.domain_hints
