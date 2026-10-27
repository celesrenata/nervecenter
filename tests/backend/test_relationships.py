"""Workstream J — People & Relationships (Personal-Ops Recovery spec §11).

Covers the frozen §11 surfaces against the *durable* plane (workstream-F
repositories + workstream-E calendar rows) on a hermetic in-memory SQLite
engine (StaticPool — no Postgres required):

- **Remembering** — notes / boundaries / birthdays on the reserved
  ``people.notes`` / ``people.boundaries`` JSONB sub-keys (normalize
  helpers + set/get round-trips, next-birthday occurrence math).
- **Unanswered conversations** — the *factual* surfacing: newest inbound
  older than N days (default 7, configurable) with no outbound reply after
  it. Threshold behaviour, reply-suppression, and the cross-person list.
- **Promises made** — only *outgoing*-sourced commitments; read/classify
  only, never re-detected.
- **Follow-up reminders** — open ``followups`` rows, due-first ordering.
- **Social plans** — explicit ``extra["attendees"]`` match or
  user-marked ``extra["social"]``; no fuzzy matching.
- **Reflective assistance** — on-request, deterministic, factual; no
  inference call, no scoring fields, no good/bad vocabulary.
- **Frozen routes** — ``GET /api/relationships/unanswered`` /
  ``/social``, ``GET`` / ``PATCH /api/people/{id}``,
  ``POST /api/people/{id}/reminders``: honest 503 without a durable
  registry, 404 on unknown persons, 201 on reminder create.

Durable-route tests follow the ``test_today.py`` pattern: the app is built
per-test with ``app.state.repository_registry`` (and a frozen-clock
``RelationshipsService``) pre-set, and ``with TestClient(app) as client:``
runs inside the test body — the full ``create_app()`` lifespan would
otherwise clear the registry (no ``NERVE_DB_URL`` in tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.app import create_app
from backend.api.endpoints import people as people_endpoints
from backend.api.endpoints import relationships as relationships_endpoints
from backend.communications import (
    ContactIdentityKind,
    get_communications_service,
    reset_communications_service,
)
from backend.communications.relationships_service import (
    BIRTHDAYS_KEY,
    DEFAULT_UNANSWERED_DAYS,
    RelationshipsService,
    normalize_boundaries,
    normalize_notes,
    next_birthday_occurrence,
)
from backend.persistence.models.base import Base
from backend.persistence.models.personal import (
    CalendarAccountRow,
    CalendarEventRow,
    CommitmentRow,
    CommunicationRow,
    ConversationRow,
    FollowupRow,
    PersonIdentityRow,
    PersonRow,
)
from backend.persistence.repositories.calendar_repo import CalendarRepository
from backend.persistence.repositories.commitments_repo import CommitmentsRepository
from backend.persistence.repositories.communications_repo import CommunicationsRepository
from backend.persistence.repositories.registry import RepositoryRegistry

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Fresh in-memory durable plane; yields (session_factory, registry,
    repo tuple, RelationshipsService bound to a frozen clock)."""
    reset_communications_service()
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        PersonRow.__table__,
        PersonIdentityRow.__table__,
        CommunicationRow.__table__,
        ConversationRow.__table__,
        CommitmentRow.__table__,
        FollowupRow.__table__,
        CalendarAccountRow.__table__,
        CalendarEventRow.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    registry = RepositoryRegistry(sessions)
    comms = CommunicationsRepository(sessions)
    commitments = CommitmentsRepository(sessions)
    calendar = CalendarRepository(sessions)
    svc = RelationshipsService(
        comms,
        commitments,
        calendar=calendar,
        session_factory=sessions,
        in_memory=get_communications_service(),
        now_fn=lambda: NOW,
    )
    yield sessions, registry, (comms, commitments, calendar), svc
    await engine.dispose()


@pytest.fixture
def client():
    """A fresh full app with no durable registry → honest 503 degradation."""
    reset_communications_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_communications_service()


@pytest.fixture
async def durable_app(db):
    """A minimal app whose ``app.state.repository_registry`` points at the
    hermetic in-memory durable plane (stands in for ``NERVE_DB_URL``).

    Built like ``test_today.wired``: only the people + relationships routers
    are mounted and the (frozen-clock) relationships service is pre-cached on
    ``app.state`` — the full ``create_app()`` wiring would clear the registry
    at startup because no DB URL is configured.
    """
    _sessions, registry, _repos, svc = db
    app = FastAPI()
    app.include_router(people_endpoints.router)
    app.include_router(relationships_endpoints.router)
    app.state.repository_registry = registry
    app.state.relationships_service = svc
    yield app


def _ingest(client: TestClient, **over) -> dict:
    payload = {
        "kind": ContactIdentityKind.EMAIL.value,
        "value": "alice.n@example.com",
        "display_name": "Alice Nguyen",
    }
    payload.update(over)
    res = client.post("/api/people/identities/ingest", json=payload)
    assert res.status_code in (200, 201), res.text
    return res.json()


# ---------------------------------------------------------------------------
# reserved JSONB normalization helpers
# ---------------------------------------------------------------------------


def test_normalize_notes_bare_list_is_legacy_notes():
    assert normalize_notes(["a", "  b ", "", None]) == {
        "notes": [{"at": None, "text": "a"}, {"at": None, "text": "b"}],
        "birthdays": [],
    }


def test_normalize_notes_preserves_reserved_and_unknown_keys():
    raw = {
        "notes": [{"text": " met at the conference", "at": "2026-01-02"}],
        "birthdays": [{"date": "1988-03-14", "name": "birthday"}],
        "custom": {"keep": True},
    }
    out = normalize_notes(raw)
    assert out["notes"] == [{"at": "2026-01-02", "text": "met at the conference"}]
    assert out[BIRTHDAYS_KEY] == [{"date": "1988-03-14", "name": "birthday"}]
    assert out["custom"] == {"keep": True}  # user data is never discarded


def test_normalize_notes_garbage_input_is_empty_bag():
    assert normalize_notes("not a dict") == {"notes": [], "birthdays": []}


def test_normalize_boundaries_bag_bare_list_and_strings():
    assert normalize_boundaries({"items": [{"rule": "no texting after 22:00"}]}) == [
        {"scope": "general", "rule": "no texting after 22:00", "active": True}
    ]
    assert normalize_boundaries(["call only"]) == [
        {"scope": "general", "rule": "call only", "active": True}
    ]
    assert normalize_boundaries(None) == []


def test_next_birthday_occurrence_this_and_next_year():
    today = datetime(2026, 10, 27).date()
    later = next_birthday_occurrence({"date": "2026-12-01"}, today=today)
    assert later["occurs_on"] == "2026-12-01"
    assert later["in_days"] == (datetime(2026, 12, 1).date() - today).days
    past = next_birthday_occurrence({"date": "2026-01-15"}, today=today)
    assert past["occurs_on"] == "2027-01-15"
    # Unparseable dates never guess a birthday.
    assert next_birthday_occurrence({"date": "soon"}, today=today) is None


# ---------------------------------------------------------------------------
# durable field round-trips (notes / boundaries / birthdays / vip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_notes_boundaries_birthdays(db):
    _sessions, _registry, (comms, _commitments, _calendar), svc = db
    person = await comms.create_person(display_name="Alice Nguyen")
    pid = person.id

    notes = await svc.set_notes(pid, ["keeps receipts", {"text": "prefers email"}])
    assert [n["text"] for n in notes["notes"]] == ["keeps receipts", "prefers email"]
    assert all(n["at"] == NOW.isoformat() for n in notes["notes"])

    appended = await svc.set_notes(pid, ["new note"], mode="append")
    assert [n["text"] for n in appended["notes"]] == [
        "keeps receipts",
        "prefers email",
        "new note",
    ]

    boundaries = await svc.set_boundaries(
        pid, ["no texting after 22:00", {"rule": "weekends off", "active": False}]
    )
    assert boundaries == [
        {"scope": "general", "rule": "no texting after 22:00", "active": True},
        {"scope": "general", "rule": "weekends off", "active": False},
    ]

    birthdays = await svc.set_birthdays(
        pid, [{"date": "1988-03-14", "name": "birthday"}, {"date": "not-a-date"}]
    )
    assert birthdays == [{"date": "1988-03-14", "name": "birthday"}]
    assert await svc.get_birthdays(pid) == birthdays

    # The reserved birthdays bag survives a notes replace.
    await svc.set_notes(pid, ["only this note"])
    profile = await svc.person_profile(pid)
    assert profile["notes"] == [{"at": NOW.isoformat(), "text": "only this note"}]
    # Occurrence math uses the service clock (frozen at 2026-10-27): the
    # 2026-03-14 birthday has passed, so the next occurrence is 2027-03-14.
    assert profile["birthdays"][0]["occurs_on"] == "2027-03-14"
    assert profile["boundaries"] == boundaries

    # VIP flag round-trip (durable).
    assert await svc.set_vip(pid, True) is True
    assert (await svc.person_profile(pid))["person"]["is_vip"] is True
    assert await svc.set_vip(pid, False) is False
    assert (await svc.person_profile(pid))["person"]["is_vip"] is False


@pytest.mark.asyncio
async def test_setters_404_on_unknown_person(db):
    _sessions, _registry, _repos, svc = db
    with pytest.raises(KeyError):
        await svc.set_notes("ghost", ["note"])
    with pytest.raises(KeyError):
        await svc.set_vip("ghost", True)


# ---------------------------------------------------------------------------
# unanswered conversations (factual, threshold-based)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unanswered_threshold_and_reply_suppression(db):
    _sessions, _registry, (comms, _commitments, _calendar), svc = db
    assert svc.unanswered_days == DEFAULT_UNANSWERED_DAYS
    alice = await comms.create_person(display_name="Alice")
    bob = await comms.create_person(display_name="Bob")

    # Alice: inbound 10 days ago, no reply → unanswered at the 7-day default.
    await comms.create_communication(
        person_id=alice.id,
        channel="email",
        direction="inbound",
        content_ref="enc://alice-in",
        received_at=NOW - timedelta(days=10),
    )
    # Bob: inbound 10 days ago but a reply 3 days later → not unanswered.
    await comms.create_communication(
        person_id=bob.id,
        channel="email",
        direction="inbound",
        content_ref="enc://bob-in",
        received_at=NOW - timedelta(days=10),
    )
    await comms.create_communication(
        person_id=bob.id,
        channel="email",
        direction="outbound",
        content_ref="enc://bob-out",
        received_at=NOW - timedelta(days=3),
    )
    # Carol: inbound 3 days ago → within the 7-day window.
    carol = await comms.create_person(display_name="Carol")
    await comms.create_communication(
        person_id=carol.id,
        channel="sms",
        direction="inbound",
        content_ref="enc://carol-in",
        received_at=NOW - timedelta(days=3),
    )

    entries = await svc.unanswered_all()
    assert [e["person_id"] for e in entries] == [alice.id]
    assert entries[0]["channel"] == "email"
    assert entries[0]["age_days"] == 10
    assert entries[0]["content_ref"] == "enc://alice-in"
    assert "display_name" in entries[0] and entries[0]["display_name"] == "Alice"

    # Per-person scoping excludes other people's conversations.
    scoped = await svc._unanswered_entries(alice.id, NOW)
    assert [e["person_id"] for e in scoped] == [alice.id]
    none_for_bob = await svc._unanswered_entries(bob.id, NOW)
    assert none_for_bob == []

    # Threshold is configurable: at 12 days even Alice (10 days) is fine,
    # but at 2 days both Alice (10 days) and Carol (3 days) qualify.
    wider = RelationshipsService(
        comms, _commitments, calendar=_calendar, session_factory=_sessions,
        in_memory=svc._in_memory, unanswered_days=12, now_fn=lambda: NOW,
    )
    assert await wider.unanswered_all() == []
    narrow = RelationshipsService(
        comms, _commitments, calendar=_calendar, session_factory=_sessions,
        in_memory=svc._in_memory, unanswered_days=2, now_fn=lambda: NOW,
    )
    assert {e["person_id"] for e in await narrow.unanswered_all()} == {
        alice.id, carol.id
    }


# ---------------------------------------------------------------------------
# promises made (outgoing-sourced commitments only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promises_are_outgoing_sourced_commitments(db):
    _sessions, _registry, (comms, commitments, _calendar), svc = db
    alice = await comms.create_person(display_name="Alice")

    outbound = await comms.create_communication(
        person_id=alice.id, channel="email", direction="outbound",
        content_ref="enc://promise-out", received_at=NOW - timedelta(days=1),
    )
    inbound = await comms.create_communication(
        person_id=alice.id, channel="email", direction="inbound",
        content_ref="enc://not-mine-in", received_at=NOW - timedelta(days=1),
    )
    await commitments.create_commitment(
        person_id=alice.id, text="send the contract",
        due_at=NOW + timedelta(days=2),
        source_communication_id=outbound.id,
    )
    await commitments.create_commitment(
        person_id=alice.id, text="they asked me for a favour",
        due_at=NOW + timedelta(days=9),
        source_communication_id=inbound.id,
    )
    await commitments.create_commitment(
        person_id=alice.id, text="a promise already kept",
        state="done", source_communication_id=outbound.id,
    )

    promises = await svc.promises_all(open_only=False)
    assert {p["text"] for p in promises} == {
        "send the contract", "a promise already kept"
    }
    assert all(p["person_id"] == alice.id for p in promises)
    assert all(p["state"] in ("open", "done") for p in promises)
    # Due-first ordering: the open (2d) one sorts before the done (undated) one.
    assert [p["state"] for p in promises] == ["open", "done"]

    open_only = await svc.promises_all(open_only=True)
    assert [p["text"] for p in open_only] == ["send the contract"]


# ---------------------------------------------------------------------------
# follow-up reminders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_up_reminders_due_first(db):
    _sessions, _registry, (comms, commitments, _calendar), svc = db
    alice = await comms.create_person(display_name="Alice")
    late = await commitments.create_followup(
        text="call about the lease", person_id=alice.id, due_at=NOW - timedelta(days=1),
        linked_refs=["enc://comm/1"],
    )
    early = await commitments.create_followup(
        text="confirm the lunch", person_id=alice.id, due_at=NOW + timedelta(days=1),
    )
    undated = await commitments.create_followup(text="someday")

    rows = await svc.follow_up_reminders()
    assert [r["id"] for r in rows] == [late.id, early.id, undated.id]
    assert rows[0]["is_due"] is True
    assert rows[0]["display_name"] == "Alice"
    assert rows[0]["linked_refs"] == ["enc://comm/1"]
    assert rows[1]["is_due"] is False
    assert rows[2]["due_at"] is None

    scoped = await svc.follow_up_reminders(person_id=alice.id)
    assert {r["id"] for r in scoped} == {late.id, early.id}

    # create_reminder writes a durable followup linked to the person.
    created = await svc.create_reminder(
        alice.id, "remind me", due_at=NOW + timedelta(days=3),
        source_ref="enc://comm/2",
    )
    assert created["person_id"] == alice.id
    assert created["linked_refs"] == ["enc://comm/2"]


# ---------------------------------------------------------------------------
# social plans (explicit attendees / user mark — no fuzzy matching)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_social_plans_explicit_attendees_only(db):
    _sessions, _registry, (comms, _commitments, calendar), svc = db
    alice = await comms.create_person(display_name="Alice")
    await comms.upsert_person_identity(
        person_id=alice.id, channel="email", handle="alice.n@example.com",
    )
    account = await calendar.create_account(provider="ical", label="home")

    # Attendee match (case-insensitive verbatim handle).
    await calendar.upsert_event(
        account_id=account.id, ical_uid="ev-dinner", title="Dinner",
        start=NOW + timedelta(days=1), end=NOW + timedelta(days=1, hours=2),
        extra={"attendees": ["Alice.N@example.com", "+14155550134"]},
    )
    # Same title/location wording, different attendee → NOT a plan.
    await calendar.upsert_event(
        account_id=account.id, ical_uid="ev-other-dinner", title="Dinner",
        start=NOW + timedelta(days=2), end=NOW + timedelta(days=2, hours=2),
        extra={"attendees": ["someone.else@example.com"]},
    )
    # User-marked social, no attendees → still a plan (any person).
    await calendar.upsert_event(
        account_id=account.id, ical_uid="ev-party", title="Party",
        start=NOW + timedelta(days=3), end=NOW + timedelta(days=3, hours=4),
        extra={"social": True},
    )

    alice_plans = await svc.social_plans(person_id=alice.id)
    assert {p["ical_uid"] for p in alice_plans} == {"ev-dinner", "ev-party"}

    # No person filter: only the explicitly marked event qualifies.
    everyone = await svc.social_plans()
    assert {p["ical_uid"] for p in everyone} == {"ev-party"}

    # A past event is outside the horizon.
    await calendar.upsert_event(
        account_id=account.id, ical_uid="ev-past", title="Old",
        start=NOW - timedelta(days=30), end=NOW - timedelta(days=29),
        extra={"social": True},
    )
    assert {p["ical_uid"] for p in await svc.social_plans()} == {"ev-party"}


@pytest.mark.asyncio
async def test_set_event_social_toggle(db):
    _sessions, _registry, (_comms, _commitments, calendar), svc = db
    account = await calendar.create_account(provider="ical", label="home")
    await calendar.upsert_event(
        account_id=account.id, ical_uid="ev", title="Thing",
        start=NOW + timedelta(days=1), end=NOW + timedelta(days=1, hours=1),
    )
    marked = await svc.set_event_social(f"{account.id}:ev", True)
    assert marked["marked_social"] is True
    unmarked = await svc.set_event_social(f"{account.id}:ev", False)
    assert unmarked["marked_social"] is False
    assert await svc.set_event_social(f"{account.id}:missing", True) is None


# ---------------------------------------------------------------------------
# person profile + reflective assistance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_profile_and_reflect_are_factual(db):
    _sessions, _registry, (comms, commitments, calendar), svc = db
    alice = await comms.create_person(display_name="Alice")
    await comms.upsert_person_identity(
        person_id=alice.id, channel="email", handle="alice.n@example.com",
    )
    await svc.set_notes(alice.id, ["met at the conference"])
    await svc.set_birthdays(alice.id, [{"date": "1988-03-14", "name": "birthday"}])
    # The promise-sourcing outbound rides a *different* channel so it does not
    # count as a reply to the email thread (reply-suppression is per channel);
    # promises stay channel-agnostic via source_communication_id.
    outbound = await comms.create_communication(
        person_id=alice.id, channel="imessage", direction="outbound",
        content_ref="enc://p", received_at=NOW - timedelta(days=2),
    )
    await comms.create_communication(
        person_id=alice.id, channel="email", direction="inbound",
        content_ref="enc://i", received_at=NOW - timedelta(days=9),
    )
    await commitments.create_commitment(
        person_id=alice.id, text="send the contract",
        due_at=NOW + timedelta(days=2), source_communication_id=outbound.id,
    )

    profile = await svc.person_profile(alice.id)
    assert profile["person"] == {
        "person_id": alice.id, "display_name": "Alice", "is_vip": False,
        "durable": True,
    }
    assert [n["text"] for n in profile["notes"]] == ["met at the conference"]
    # Frozen clock 2026-10-27 → the 03-14 birthday rolls into 2027.
    assert profile["birthdays"][0]["occurs_on"] == "2027-03-14"
    assert len(profile["identities"]) == 1
    assert profile["unanswered"][0]["channel"] == "email"
    assert profile["unanswered"][0]["age_days"] == 9
    assert [p["text"] for p in profile["promises_made"]] == ["send the contract"]
    assert [c["text"] for c in profile["commitments"]] == ["send the contract"]
    # No scoring field anywhere in the record.
    assert not any("score" in k for k in profile)

    unknown = await svc.person_profile("ghost")
    assert unknown["person"] is None

    reflection = await svc.reflect(alice.id, "Am I keeping in touch?")
    assert reflection["question"] == "Am I keeping in touch?"
    assert reflection["facts"]["note_count"] == 1
    assert reflection["facts"]["birthday_count"] == 1
    assert reflection["facts"]["open_commitments"] == 1
    assert reflection["facts"]["promises_made"] == 1
    assert reflection["facts"]["unanswered_conversations"] == 1
    assert reflection["facts"]["interactions_last_30_days"] == 2
    assert reflection["facts"]["last_contact_at"] == (NOW - timedelta(days=2)).isoformat()
    # Factual framing lives in the note; the summary is pure counts.
    assert "recorded facts" in reflection["note"]
    assert await svc.reflect("ghost", "?") is None


# ---------------------------------------------------------------------------
# frozen API routes — honest 503 without a durable registry
# ---------------------------------------------------------------------------


def test_relationships_routes_503_without_durable_registry(client):
    assert client.get("/api/relationships/unanswered").status_code == 503
    assert client.get("/api/relationships/social").status_code == 503
    assert client.patch("/api/people/ghost", json={}).status_code == 503
    assert (
        client.post("/api/people/ghost/reminders", json={"text": "x"}).status_code
        == 503
    )
    # The in-memory person record still serves (relationships omitted).
    body = _ingest(client)
    res = client.get(f"/api/people/{body['person']['person_id']}")
    assert res.status_code == 200
    assert "relationships" not in res.json()


# ---------------------------------------------------------------------------
# frozen API routes — against the hermetic durable plane
# ---------------------------------------------------------------------------


def test_get_person_includes_relationships_profile(durable_app):
    with TestClient(durable_app) as client:
        body = _ingest(client)
        pid = body["person"]["person_id"]
        res = client.get(f"/api/people/{pid}")
        assert res.status_code == 200
        payload = res.json()
        assert payload["item"]["person_id"] == pid
        rel = payload["relationships"]
        assert rel["person"]["person_id"] == pid
        assert rel["person"]["display_name"] == "Alice Nguyen"
        for key in (
            "identities", "notes", "boundaries", "birthdays", "commitments",
            "promises_made", "unanswered", "follow_ups", "social_plans",
        ):
            assert key in rel
        assert "score" not in rel

        assert client.get("/api/people/ghost").status_code == 404


def test_patch_person_edits_durable_fields(durable_app):
    with TestClient(durable_app) as client:
        body = _ingest(client)
        pid = body["person"]["person_id"]
        res = client.patch(
            f"/api/people/{pid}",
            json={
                "is_vip": True,
                "notes": ["keep in touch quarterly"],
                "boundaries": ["weekends off"],
                "birthdays": [{"date": "1988-03-14"}],
            },
        )
        assert res.status_code == 200, res.text
        payload = res.json()
        # The in-memory-only person was promoted to a durable row on write.
        assert payload["item"]["person_id"] == pid
        assert payload["item"]["is_vip"] is True
        assert payload["notes"] == [
            {"at": NOW.isoformat(), "text": "keep in touch quarterly"}
        ]
        assert payload["boundaries"] == [
            {"scope": "general", "rule": "weekends off", "active": True}
        ]
        # PATCH returns the occurrence-augmented profile shape, not the raw bag.
        assert [b["date"] for b in payload["birthdays"]] == ["1988-03-14"]
        assert payload["birthdays"][0]["occurs_on"] == "2027-03-14"

        # The VIP flag also lands in the durable row.
        assert (
            client.get(f"/api/people/{pid}").json()["relationships"]["person"]["is_vip"]
            is True
        )

        assert client.patch("/api/people/ghost", json={"is_vip": True}).status_code == 404


def test_reminders_route_creates_durable_followup(durable_app):
    with TestClient(durable_app) as client:
        body = _ingest(client)
        pid = body["person"]["person_id"]
        res = client.post(
            f"/api/people/{pid}/reminders",
            json={"text": "call about the lease", "due_at": "2026-11-02T09:00:00+00:00"},
        )
        assert res.status_code == 201, res.text
        item = res.json()["item"]
        assert item["person_id"] == pid
        assert item["state"] == "open"
        assert item["due_at"].startswith("2026-11-02")

        assert client.post("/api/people/ghost/reminders", json={"text": "x"}).status_code == 404
        # Validation: empty text is rejected.
        assert client.post(f"/api/people/{pid}/reminders", json={"text": "  "}).status_code == 422


async def test_unanswered_and_social_routes_with_data(durable_app, db):
    # Seed the durable plane directly (the app reads through the same
    # repository registry it carries).
    _sessions, _registry, (comms, _commitments, _calendar), _svc = db
    person = await comms.create_person(display_name="Durable Dan")
    await comms.create_communication(
        person_id=person.id, channel="email", direction="inbound",
        content_ref="enc://dan", received_at=NOW - timedelta(days=8),
    )
    account = await _calendar.create_account(provider="ical", label="home")
    await _calendar.upsert_event(
        account_id=account.id, ical_uid="ev", title="Social",
        start=NOW + timedelta(days=1), end=NOW + timedelta(days=2),
        extra={"social": True},
    )

    with TestClient(durable_app) as client:
        res = client.get("/api/relationships/unanswered")
        assert res.status_code == 200
        entries = res.json()["data"]
        assert len(entries) == 1
        assert entries[0]["person_id"] == person.id
        assert entries[0]["display_name"] == "Durable Dan"
        assert entries[0]["age_days"] == 8
        assert entries[0]["content_ref"] == "enc://dan"

        # Person-scoped filter (entries keep their person_id).
        scoped = client.get(
            "/api/relationships/unanswered",
            params={"person_id": person.id},
        ).json()["data"]
        assert [e["person_id"] for e in scoped] == [person.id]
        other = client.get(
            "/api/relationships/unanswered", params={"person_id": "nobody"}
        ).json()["data"]
        assert other == []

        social = client.get("/api/relationships/social").json()["data"]
        assert {p["ical_uid"] for p in social} == {"ev"}
        assert social[0]["marked_social"] is True
