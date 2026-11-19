"""Per-category Ask coverage (C1..C12 of spec ask-nervcenter-answer-coverage).

One seeded world (fake services, fixed clock, scripted EchoModel) is asked the example
question of each category through the real ``/api/context/ask`` endpoint. Every test asserts a
value computed from the seeded data and that each citation resolves to a record retrieved for
that question. Fakes only; no network.
"""
from __future__ import annotations

import contextlib
import os
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import httpx

import pytest

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval.sources import ADAPTERS
from backend.context.retrieval.sources import status as status_mod
from backend.context.retrieval.sources.logs import logs as logs_adapter
from backend.persistence.repositories.obligations_repo import ObligationsRepository
from backend.persistence.repositories.routines_repo import RoutinesRepository
from backend.routines.service import RoutinesService
from backend.wellness.service import WellnessService
from backend.briefing.service import BriefingService

from test_ask_answer_coverage_exploration import (
    REFUSAL, EchoModel, Run, check_grounded, harness, mentions,
)
from test_ask_retrieval import NOW  # Wednesday 2026-06-03 12:00 UTC


def day(offset: int, hour: int = 12) -> Any:
    return (NOW + timedelta(days=offset)).replace(hour=hour)


# -- seeded world ------------------------------------------------------------------------------
BILLS = [
    {"id": "b-power", "payee": "Acme Power", "amount_cents": "12000", "currency": "USD",
     "status": "pending", "due_at": day(2).isoformat()},
    {"id": "b-water", "payee": "Birch Water", "amount_cents": "4550", "currency": "USD",
     "status": "pending", "due_at": day(-3).isoformat()},
    {"id": "b-rent", "payee": "Cedar Rent", "amount_cents": "150000", "currency": "USD",
     "status": "pending", "due_at": day(20).isoformat()},
    {"id": "b-net", "payee": "Delta Internet", "amount_cents": "8000", "currency": "USD",
     "status": "confirmed_paid", "due_at": day(-15).isoformat()},  # 2026-05-19
]


def _task(i: str, title: str, priority: int, status: str = "pending", due: int | None = None):
    return SimpleNamespace(id=i, title=title, priority=priority, status=status,
                           due_at=day(due) if due is not None else None, created_at=day(-5),
                           goal_id=None, dependencies=[], inputs={})


def _goal(i: str, title: str, deadline: int | None, progress: float):
    return SimpleNamespace(id=i, title=title, status="active", progress=progress, priority=1,
                           deadline=day(deadline) if deadline is not None else None, created_at=day(-60))


def _obligation(i: str, title: str, otype: str, due: int | None, priority: int = 1):
    return SimpleNamespace(
        id=i, title=title, obligation_type=otype, status="open", origin="stated",
        due_at=day(due) if due is not None else None, created_at=day(-3), consequence=None,
        next_action=None, priority=priority)


TASKS = [_task("t-1", "File quarterly estimate", 9, due=4), _task("t-2", "Renew passport", 3),
         _task("t-4", "Gather receipts for taxes", 2),
         _task("t-3", "Water the plants", 1, status="completed")]
GOALS = [_goal("g-1", "Run a half marathon", -10, 0.4), _goal("g-2", "Read twelve books", 200, 0.5)]
OBLIGATIONS = [_obligation("o-1", "Send Henderson the site plan", "owed_by_me", 3, 8),
               _obligation("o-2", "Mortgage statement from Orla", "waiting_on", 5),
               _obligation("o-3", "Sign tax return for Quinn CPA", "owed_by_me", 9)]
FOLLOWUPS = [SimpleNamespace(id="f-1", reason="Ask Henderson about permits", target_kind="person",
                             status="pending", due_at=day(1))]
WELLNESS = [
    {"id": f"w{d}", "mood": mood, "energy": 5, "stress": 3, "sleep_minutes": sleep,
     "journal_entry": "", "at": (NOW - timedelta(days=d)).isoformat()}
    for d, mood, sleep in [(1, 6, 420), (2, 7, 360), (4, 5, 480),       # this week (Mon Jun 1 onwards: Jun 2, 1)
                           (10, 8, 400), (15, 4, 400), (20, 6, 400)]    # May
]


class Incidents:
    async def list_incidents(self):
        return [{"id": "i-1", "title": "Disk full on nas", "severity": "high", "status": "open",
                 "createdAt": NOW.isoformat()},
                {"id": "i-2", "title": "Cert expiring", "severity": "low", "status": "open",
                 "createdAt": NOW.isoformat()},
                {"id": "i-3", "title": "Old outage", "severity": "low", "status": "resolved"}]


class Systems:
    async def list_systems(self):
        return [{"name": "gateway", "status": "ok", "message": "healthy", "checked_at": NOW.isoformat()},
                {"name": "nas", "status": "down", "message": "unreachable", "checked_at": NOW.isoformat()}]


class Tasks:
    async def list_tasks(self, *, status=None, goal_id=None, limit=100):
        return TASKS[:limit]


class Goals:
    async def list_goals(self, *, limit=100, **kw):
        return GOALS[:limit]


def _es_handler(request: httpx.Request) -> httpx.Response:
    hit = {"_source": {"@timestamp": (NOW - timedelta(hours=1)).isoformat(),
                       "log": "error: upstream timeout talking to nas",
                       "kubernetes": {"namespace_name": "media", "pod_name": "jellyfin-0"}}}
    return httpx.Response(200, json={"hits": {"hits": [hit], "total": {"value": 1}}})


async def _logs(state, intent, limit):
    return await logs_adapter(state, intent, limit, es_transport=httpx.MockTransport(_es_handler))


async def _mail_security(state, now):
    return [("status", "mail-security:summary", "Mail security status",
             "stages: 3 ok; 0 queued; gate holds: 4 held", now)]


@contextlib.contextmanager
def world() -> Iterator[Run]:
    async def list_entries(self, *, from_dt=None, to_dt=None, **kw):
        return [e for e in WELLNESS
                if (from_dt is None or e["at"] >= from_dt.isoformat())
                and (to_dt is None or e["at"] < to_dt.isoformat())]

    async def obligations_list(self, **kw):
        return OBLIGATIONS

    async def list_routines(self, *, enabled_only=False):
        return [{"id": "r-1", "title": "Morning stretch", "kind": "habit", "schedule": {"times": ["07:00"]}},
                {"id": "r-2", "title": "Evening walk", "kind": "habit", "schedule": {}}]

    async def list_check_ins(self, routine_id, *, since=None):
        return [SimpleNamespace(at=NOW.replace(hour=7), completed=True, note="")] if routine_id == "r-1" else []

    async def fetch_knowledge(factory):
        notes = [SimpleNamespace(id="n-1", author="me", body="The lease renews in March.",
                                 created_at=NOW - timedelta(days=3)),
                 SimpleNamespace(id="n-2", author="me", body="Decision: we chose Acme Power for the Henderson project.",
                                 created_at=NOW - timedelta(days=4))]
        return [], [], [], [(n, "Home") for n in notes]

    with harness(BILLS) as run, \
            patch.object(WellnessService, "list_entries", list_entries), \
            patch.object(ObligationsRepository, "list", obligations_list), \
            patch.object(RoutinesService, "list_routines", list_routines), \
            patch.object(RoutinesRepository, "list_check_ins", list_check_ins), \
            patch.object(status_mod, "_mail_security", _mail_security), \
            patch("backend.context.retrieval.sources.knowledge._fetch", fetch_knowledge), \
            patch.dict(ADAPTERS, {"logs": _logs}), \
            patch.dict(os.environ, {"NERVE_LOGS_ES_URL": "http://logs.invalid"}):
        state = run.client.app.state
        state.services = {"systems": Systems(), "incidents": Incidents(), "tasks": Tasks(), "goals": Goals()}
        state.followup_service = SimpleNamespace(list=lambda: FOLLOWUPS)
        state.briefing_service = BriefingService(
            attention=lambda: state.waiting_on_service._attention, email=lambda: state.email_service,
            messages=lambda: state.messages_service, voicemail=lambda: state.voicemail_service,
            waiting_on=lambda: state.waiting_on_service, calendar=lambda: state.calendar_service)
        _seed_comms(state)
        yield run


def _seed_comms(state: Any) -> None:
    state.email_service.ingest({
        "message_id": "m-2", "from_address": "orla@example.test", "sender_name": "Orla Finch",
        "to_address": "me@example.test", "subject": "Mortgage statement", "body": "Statement attached.",
        "timestamp": (NOW - timedelta(hours=5)).isoformat()})
    state.email_service.ingest({
        "message_id": "m-3", "from_address": "henderson@example.test", "sender_name": "Dana Henderson",
        "to_address": "me@example.test", "subject": "Henderson project kickoff", "body": "Kickoff is set.",
        "timestamp": (NOW - timedelta(days=1)).isoformat()})
    state.email_service.ingest({
        "message_id": "m-4", "from_address": "quinn@cpa.example.test", "sender_name": "Quinn CPA",
        "to_address": "me@example.test", "subject": "Your 2025 tax return is ready to sign",
        "body": "Please review.", "timestamp": (NOW - timedelta(days=6)).isoformat()})
    for t in state.email_service.list_threads():  # the water invoice and the CPA mail are read
        if t.subject.startswith(("Water", "Your 2025")):
            state.email_service.acknowledge(t.thread_id)
    state.messages_service.ingest({
        "message_id": "t-2", "peer_identifier": "+15550100", "peer_name": "Sam",
        "body": "Running late, see you at 7.", "timestamp": (NOW - timedelta(hours=1)).isoformat()})
    state.voicemail_service.ingest({
        "voicemail_id": "v-2", "caller_phone": "+15550122", "caller_name": "Pat Rivera",
        "transcript": "Please call me back about the invoice.",
        "received_at": (NOW - timedelta(hours=2)).isoformat(), "duration_seconds": 15})
    state.contacts_service.ingest({
        "person_id": "p-2", "display_name": "Dana Henderson", "emails": ["henderson@example.test"],
        "phones": ["+15550777"]})
    state.waiting_on_service.ingest({
        "request_id": "wo-1", "person_display_name": "Orla Finch", "description": "mortgage statement",
        "requested_at": (NOW - timedelta(days=3)).isoformat()})
    state.calendar_service.ingest({
        "event_id": "ev-2", "title": "Dentist checkup", "start": day(3, 9).isoformat(),
        "end": day(3, 10).isoformat(), "source": "test"})
    state.calendar_service.ingest({
        "event_id": "ev-3", "title": "Henderson project review", "start": day(7, 14).isoformat(),
        "end": day(7, 15).isoformat(), "source": "test"})


def ask(run: Run, question: str, **kw: Any) -> dict[str, Any]:
    """Ask, then check: no refusal, at least one citation, every citation retrieved and resolved."""
    body = run.ask(question, **kw)
    check_grounded(run, body)
    assert body["citations"], f"no citation for {question!r}: {body['answer']!r}"
    assert all(c.get("title") for c in body["citations"]), body["citations"]  # resolved to a record
    return body


def cites(body: dict[str, Any], title_part: str) -> bool:
    return any(title_part in (c.get("title") or "") for c in body["citations"])


def computed(body: dict[str, Any]) -> str:
    line = next((ln for ln in body["answer"].splitlines() if ln.startswith("COMPUTED:")), "")
    assert line, f"no computed line: {body['answer']!r}"
    return line


def save_turns(run: Run, sid: str, turns: list[tuple[str, dict[str, Any]]]) -> None:
    messages: list[dict[str, Any]] = []
    for n, (question, body) in enumerate(turns):
        messages.append({"id": f"u{n}", "role": "user", "content": question, "timestamp": NOW.isoformat()})
        messages.append({"id": f"a{n}", "role": "assistant", "content": body["answer"],
                         "timestamp": NOW.isoformat(), "citations": body["citations"],
                         "records": body["records"]})
    saved = run.client.put("/api/ask/sessions", json={
        "active_id": sid, "sessions": [{"id": sid, "title": "t", "messages": messages}]})
    assert saved.status_code == 200, saved.text


# -- C1 bills and spending -------------------------------------------------------------------------
def test_c1_bills_and_spending() -> None:
    with world() as run:
        body = ask(run, "what is my biggest bill?")
        assert "Cedar Rent ($1,500.00)" in computed(body) and cites(body, "Cedar Rent")
        body = ask(run, "how much do I owe in total?")  # 120.00 + 45.50 + 1500.00, paid bill excluded
        assert "$1,665.50" in computed(body) and cites(body, "Acme Power")
        body = ask(run, "which bills are overdue?")
        line = computed(body)
        assert "1 overdue" in line and "Birch Water" in line and "$45.50" in line
        assert cites(body, "Birch Water") and not cites(body, "Acme Power")
        body = ask(run, "what bills are due this week?")
        assert "Acme Power" in computed(body) and "$120.00" in computed(body) and cites(body, "Acme Power")
        body = ask(run, "how much did I pay Delta Internet last month?")
        assert "$80.00" in computed(body) and cites(body, "Delta Internet")


# -- C2 email ------------------------------------------------------------------------------------
def test_c2_email() -> None:
    with world() as run:
        body = ask(run, "what email did I get from Orla?")
        assert cites(body, "Mortgage statement") and "Statement attached" in body["answer"]
        body = ask(run, "any email about the Henderson project?")
        assert cites(body, "Henderson project kickoff")
        body = ask(run, "how many unread emails do I have?")  # 4 threads, 2 acknowledged
        assert "There are 2 matching mail" in computed(body) and cites(body, "Mortgage statement")
        body = ask(run, "latest email from Dana Henderson")
        assert "Henderson project kickoff on 2026-06-02" in computed(body)
        assert cites(body, "Henderson project kickoff")


# -- C3 calendar ----------------------------------------------------------------------------------
def test_c3_calendar() -> None:
    with world() as run:
        body = ask(run, "what is on my calendar tomorrow?")
        assert cites(body, "Piano lesson with Marta") and not cites(body, "Dentist checkup")
        body = ask(run, "when is my next dentist appointment?")
        assert "Dentist checkup on 2026-06-06" in computed(body) and cites(body, "Dentist checkup")
        body = ask(run, "am I free tomorrow afternoon?")  # the 12:00-13:00 lesson is the conflict
        assert cites(body, "Piano lesson with Marta") and "12:00-13:00" in body["answer"]
        body = ask(run, "how many meetings do I have next week?")
        assert "There are 1 matching calendar" in computed(body) and cites(body, "Henderson project review")


# -- C4 texts, relay, voicemail ------------------------------------------------------------------
def test_c4_texts_and_voicemail() -> None:
    with world() as run:
        body = ask(run, "any voicemail today?")
        assert cites(body, "Voicemail from Pat Rivera") and not cites(body, "Voicemail from Dentist office")
        body = ask(run, "what was the last text from Sam?")
        first = next(ln for ln in body["answer"].splitlines() if "Text from Sam" in ln)
        assert "Running late" in first and cites(body, "Text from Sam")
        body = ask(run, "who called me back?")
        assert cites(body, "Voicemail from Pat Rivera") and "call me back" in body["answer"]


# -- C5 people and contacts ------------------------------------------------------------------------
def test_c5_people() -> None:
    with world() as run:
        body = ask(run, "what is Dana Henderson's phone number?")
        assert "+15550777" in body["answer"] and cites(body, "Dana Henderson")
        body = ask(run, "who is waiting on me?")
        assert cites(body, "Send Henderson the site plan")
        body = ask(run, "who am I waiting on?")
        assert cites(body, "Waiting on Orla Finch") and "mortgage statement" in body["answer"]


# -- C6 tasks, goals, follow-ups, obligations -------------------------------------------------------
def test_c6_tasks_goals_obligations() -> None:
    with world() as run:
        body = ask(run, "what are my open tasks?")
        for title in ("File quarterly estimate", "Renew passport", "Gather receipts for taxes"):
            assert cites(body, title), (title, body["answer"])
        assert not cites(body, "Water the plants")  # completed
        body = ask(run, "what is my highest priority task?")
        assert "File quarterly estimate (priority 9)" in computed(body) and cites(body, "File quarterly estimate")
        body = ask(run, "what do I owe people?")
        assert cites(body, "Send Henderson the site plan") and cites(body, "Sign tax return for Quinn CPA")
        body = ask(run, "which goals am I behind on?")
        assert "Run a half marathon" in computed(body) and cites(body, "Run a half marathon")
        assert not cites(body, "Read twelve books")


# -- C7 documents and knowledge ----------------------------------------------------------------------
def test_c7_knowledge() -> None:
    with world() as run:
        body = ask(run, "what do my notes say about the lease?")
        assert "The lease renews in March" in body["answer"] and cites(body, "Note: Home")
        body = ask(run, "what decisions did I record?")
        assert "we chose Acme Power" in body["answer"]
        body = ask(run, "find the document about the lease")
        assert "The lease renews in March" in body["answer"]


# -- C8 health, wellness, routines -----------------------------------------------------------------
def test_c8_health_and_routines() -> None:
    with world() as run:
        body = ask(run, "how did I sleep this week?")  # Jun 1 360 min, Jun 2 420 min
        assert "average 6.5 h (390 min)" in body["answer"] and cites(body, "Health summary: sleep")
        body = ask(run, "what was my average mood last month?")  # May: 5, 8, 4, 6
        assert "average 5.8" in body["answer"] and cites(body, "Health summary: mood")
        body = ask(run, "did I do my morning routine today?")
        assert "completed on 2026-06-03" in body["answer"] and cites(body, "Routine: Morning stretch")


# -- C9 security and ops status -----------------------------------------------------------------------
def test_c9_status() -> None:
    with world() as run:
        body = ask(run, "is anything wrong right now?")
        assert "2 open (1 high, 1 low)" in body["answer"] and cites(body, "Open incidents")
        assert cites(body, "System: nas") and "down" in body["answer"]
        body = ask(run, "any open incidents?")
        assert cites(body, "Disk full on nas") and cites(body, "Cert expiring")
        assert not cites(body, "Old outage")
        body = ask(run, "how many emails were held today?")  # counts only, from the status summary
        assert "gate holds: 4 held" in body["answer"] and cites(body, "Mail security status")
        assert "COMPUTED" not in body["answer"]
        body = ask(run, "which systems are down?")
        assert "1 down" in body["answer"] and cites(body, "System: nas")
        body = ask(run, "any recent errors?")
        assert "upstream timeout" in body["answer"] and cites(body, "media/jellyfin-0")


# -- C10 briefing and overview ---------------------------------------------------------------------------
def test_c10_briefing() -> None:
    with world() as run:
        body = ask(run, "what do I need to know today?")
        assert cites(body, "Needs attention: Call Pat Rivera back") and cites(body, "Open incidents")
        body = ask(run, "what needs my attention?")
        assert cites(body, "Needs attention: Reply to Sam")
        body = ask(run, "summarize my week")
        assert cites(body, "Overnight activity") and "2 voicemails" in body["answer"]


# -- C11 cross-domain -----------------------------------------------------------------------------------------
def test_c11_cross_domain() -> None:
    from backend.context.retrieval.ranker import MAX_RECORDS
    with world() as run:
        for question, wanted in (
            ("everything about the Henderson project", {"calendar", "mail", "contacts", "knowledge",
                                                        "followups", "obligations"}),
            ("what is going on with my taxes?", {"mail", "obligations", "tasks"}),
        ):
            body = ask(run, question)
            domains = {c["domain"] for c in body["citations"]}
            assert wanted <= domains, (question, domains)
            assert len(body["citations"]) <= MAX_RECORDS


# -- C12 follow-ups and chat --------------------------------------------------------------------------------
def test_c12_follow_ups() -> None:
    with world() as run:
        first = ask(run, "what is my biggest bill?", session_id="s1")
        save_turns(run, "s1", [("what is my biggest bill?", first)])
        due = ask(run, "when is it due?", session_id="s1")
        assert "2026-06-23" in due["answer"] and cites(due, "Cedar Rent")
        assert not cites(due, "Acme Power")
        save_turns(run, "s1", [("what is my biggest bill?", first)])
        after = ask(run, "and the one after that", session_id="s1")
        assert cites(after, "Acme Power") and not cites(after, "Cedar Rent")  # second biggest, $120.00


@pytest.mark.parametrize("question", ["what can you do?", "hello"])
def test_c12_chat_answers_capabilities_without_retrieval(question: str) -> None:
    with world() as run:
        body = run.ask(question)
        answer = body["answer"].lower()
        assert REFUSAL not in answer and "calendar" in answer and "bills" in answer
        assert body["citations"] == [] and body["records"] == [] and run.model.prompts == []
        assert all(not r.searched and not r.records for r in run.seen)  # no source was read
