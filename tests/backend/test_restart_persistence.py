"""Restart-semantics test for the durable personal plane (workstream F).

Proves the Wave-1 contract that workstreams B (connector control plane) and C
(mail ingestion) build on: personal-plane records written through the
repositories survive a simulated process restart — the engine is disposed and
a fresh engine + session factory are created the same way the app wiring does,
and every record (ids, enum-like values, timestamps, JSONB) is read back
unchanged.

The database is a **file-backed** SQLite scratch DB (``aiosqlite``), mirroring
the house test convention (``tests/backend/test_evidence.py`` /
``tests/backend/registry/test_service.py``). A *file* DB — not in-memory — is
used deliberately: ``dispose()`` must not drop the data, which is exactly what
a real restart relies on.

Also asserts the migration contract: the fourteen frozen tables exist after
``Base.metadata.create_all`` and match the ``op.create_table`` set declared in
``006_personal_plane.py``.

``test_wave_three_survives_restart`` extends the same contract to the
wave-3 planes — bills + bill candidates (G), routines + check-ins + sleep
records (H), wellness entries + preferences (I), and calendar accounts +
events (E) — written through their own repositories and read back through
the same list/query methods the endpoints use.
"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from typing import Any
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.models.bills import BillCandidateRow, BillRow
from backend.persistence.models.personal import (
    AttentionItemRow,
    CalendarAccountRow,
    CalendarEventRow,
    CommunicationRow,
    CommitmentRow,
    ConnectionRow,
    ConversationRow,
    FollowupRow,
    ImportJobRow,
    IngestCursorRow,
    NotificationLogRow,
    PersonIdentityRow,
    PersonRow,
    PreferenceRow,
    RelaySeenMessageRow,
    SecretRow,
    SnoozeRow,
)
from backend.persistence.models.routines import (
    RoutineCheckInRow,
    RoutineRow,
    SleepRecordRow,
)
from backend.persistence.models.wellness import WellnessEntryRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.bills_repo import BillsRepository
from backend.persistence.repositories.connections_repo import IdentitySecretCodec
from backend.persistence.repositories.routines_repo import RoutinesRepository
from backend.persistence.repositories.wellness_repo import (
    EXPORT_LOG_KEY,
    PREF_ENABLED,
    WellnessRepository,
)

#: The fourteen frozen personal-plane tables (spec §6).
PERSONAL_TABLES = [
    PersonRow.__table__,
    PersonIdentityRow.__table__,
    CommunicationRow.__table__,
    ConversationRow.__table__,
    CommitmentRow.__table__,
    FollowupRow.__table__,
    AttentionItemRow.__table__,
    SnoozeRow.__table__,
    NotificationLogRow.__table__,
    PreferenceRow.__table__,
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
]

#: The table names the ORM registers (used for the schema match assertion).
PERSONAL_TABLE_NAMES = sorted(t.name for t in PERSONAL_TABLES)

#: The wave-3 tables layered on top of the wave-1 plane — bills/candidates
#: (G), routines/check-ins/sleep (H), wellness entries (I), and calendar
#: accounts + events (E). ``PreferenceRow`` (used by the wellness plane) is
#: already part of ``PERSONAL_TABLES`` and is not repeated here.
WAVE3_TABLES = [
    BillRow.__table__,
    BillCandidateRow.__table__,
    RoutineRow.__table__,
    RoutineCheckInRow.__table__,
    SleepRecordRow.__table__,
    WellnessEntryRow.__table__,
    CalendarAccountRow.__table__,
    CalendarEventRow.__table__,
]


def _same_time(a, b) -> bool:
    """Compare two timestamps without the tzinfo.

    The scratch DB is SQLite, which drops timezone awareness on read-back
    even though the column is ``DateTime(timezone=True)`` (Postgres
    ``timestamptz`` preserves it). The restart test asserts on wall-clock
    equality; on a real Postgres the values would compare directly.
    """
    if a is None or b is None:
        return a is b
    return a.replace(tzinfo=None) == b.replace(tzinfo=None)


async def _make_engine(
    db_path: Path, extra_tables: list[Any] | None = None
) -> AsyncEngine:
    """Build a durable, file-backed engine the way the app wiring does.

    ``extra_tables`` adds wave-3 tables on top of the frozen wave-1 set
    (``create_all`` takes an explicit table list, so a fresh scratch DB only
    gets what is passed).
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = list(PERSONAL_TABLES) + list(extra_tables or ())
    async with engine.begin() as conn:
        await conn.run_sync(
            PERSONAL_TABLES[0].metadata.create_all, tables=tables
        )
    return engine


def _build_registry(engine: AsyncEngine) -> RepositoryRegistry:
    """Construct the registry exactly as ``backend.api.wiring`` does."""
    from backend.persistence.connection import make_session_factory

    return RepositoryRegistry(
        make_session_factory(engine),
        secret_codec=IdentitySecretCodec(),
    )


class TestRestartSemantics:
    """Write → dispose engine → re-open → verify records are unchanged."""

    async def test_records_survive_restart(self, tmp_path: Path):
        db_path = tmp_path / "personal_plane.db"
        engine = await _make_engine(db_path)
        registry = _build_registry(engine)

        # -- Phase 1: write the durable boundary through the repositories. ----
        conn = await registry.connections.create_connection(
            kind="gmail",
            label="Personal Gmail",
            config={"imap_host": "imap.gmail.com", "account": "me@gmail.com"},
            auth_status="pairing_pending",
            operational_status="never_synced",
            enabled=True,
        )
        secret = await registry.connections.upsert_secret(
            kind="app_password",
            owner_kind="connection",
            owner_id=conn.id,
            plaintext="s3cret-app-password",
        )
        person = await registry.communications.create_person(
            display_name="Ada Lovelace",
            vip=True,
            default_sensitivity="PERSONAL",
            notes={"context": "old friend"},
            boundaries={"no_work_messages": True},
        )
        await registry.communications.upsert_person_identity(
            person_id=person.id, channel="email", handle="ada@example.com",
            resolved=True,
        )
        comm = await registry.communications.create_communication(
            person_id=person.id,
            channel="email",
            direction="incoming",
            content_ref="content://store/abc123",
            subject="Lunch?",
            sensitivity="PERSONAL",
            received_at=datetime(2026, 10, 1, 12, 30, tzinfo=UTC),
            flags={"unread": True, "has_deadline": False},
        )
        await registry.communications.upsert_conversation(
            person_id=person.id,
            channel="email",
            last_message_at=datetime(2026, 10, 1, 12, 31, tzinfo=UTC),
            unread_count=1,
        )
        commitment = await registry.commitments.create_commitment(
            person_id=person.id,
            text="Send Ada the contract by Friday",
            due_at=datetime(2026, 10, 5, 0, 0, tzinfo=UTC),
            state="open",
            evidence={"quote": "I'll get the contract to you"},
            source_communication_id=comm.id,
        )
        attention = await registry.attention.create_attention_item(
            title="Contract due Friday",
            importance=8,
            urgency="high",
            body="Ada is waiting on the signed contract.",
            deadline_at=datetime(2026, 10, 5, 0, 0, tzinfo=UTC),
            source_refs=[commitment.id, comm.id],
            recommended_action="Sign and send the contract",
            escalation_policy={"notify_on": ["critical"]},
            state="active",
            resolution_condition="contract sent",
        )
        await registry.attention.record_notification(
            attention.id, level="high", channel="push"
        )
        cursor1 = await registry.imports.set_cursor(
            conn.id, "gmail_history", {"history_id": 42}
        )
        job = await registry.imports.create_import_job(
            conn.id, "mail_history", total=1000
        )

        # Snapshot the values we must see unchanged after the restart.
        snapshot = {
            "conn": (conn.id, conn.kind, conn.auth_status,
                     conn.operational_status, conn.enabled,
                     conn.config["imap_host"]),
            "secret": (secret.id, secret.owner_id, secret.owner_kind,
                       secret.payload),
            "person": (person.id, person.display_name, person.vip,
                       person.default_sensitivity, person.notes,
                       person.boundaries),
            "comm": (comm.id, comm.person_id, comm.channel, comm.direction,
                     comm.content_ref, comm.sensitivity, comm.flags),
            "commitment": (commitment.id, commitment.person_id,
                           commitment.text, commitment.state,
                           commitment.evidence,
                           commitment.source_communication_id),
            "attention": (attention.id, attention.title, attention.importance,
                          attention.urgency, attention.state,
                          attention.source_refs, attention.escalation_policy,
                          attention.resolution_condition),
            "cursor": (cursor1.id, cursor1.connection_id, cursor1.kind,
                       cursor1.cursor_value),
            "job": (job.id, job.connection_id, job.kind, job.state, job.total),
        }

        # -- Phase 2: simulate a process restart (dispose the engine). -------
        await engine.dispose()

        # -- Phase 3: re-create engine + factory the way the app would. ------
        engine2 = await _make_engine(db_path)
        registry2 = _build_registry(engine2)

        # Every record survives unchanged.
        conn2 = await registry2.connections.get_connection(conn.id)
        assert conn2 is not None
        assert (conn2.id, conn2.kind, conn2.auth_status,
                conn2.operational_status, conn2.enabled,
                conn2.config["imap_host"]) == snapshot["conn"]

        assert await registry2.connections.get_secret(
            kind="app_password", owner_kind="connection", owner_id=conn.id
        ) == "s3cret-app-password"

        person2 = await registry2.communications.get_person(person.id)
        assert person2 is not None
        assert (person2.id, person2.display_name, person2.vip,
                person2.default_sensitivity, person2.notes,
                person2.boundaries) == snapshot["person"]

        comms = await registry2.communications.list_communications(
            person_id=person.id
        )
        assert len(comms) == 1
        c2 = comms[0]
        assert (c2.id, c2.person_id, c2.channel, c2.direction,
                c2.content_ref, c2.sensitivity, c2.flags) == snapshot["comm"]
        assert _same_time(c2.received_at, comm.received_at)

        commitment2 = await registry2.commitments.get_commitment(commitment.id)
        assert commitment2 is not None
        assert (commitment2.id, commitment2.person_id, commitment2.text,
                commitment2.state, commitment2.evidence,
                commitment2.source_communication_id) == snapshot["commitment"]
        assert _same_time(commitment2.due_at, commitment.due_at)

        attention2 = await registry2.attention.get_attention_item(attention.id)
        assert attention2 is not None
        assert (attention2.id, attention2.title, attention2.importance,
                attention2.urgency, attention2.state,
                attention2.source_refs, attention2.escalation_policy,
                attention2.resolution_condition) == snapshot["attention"]
        assert _same_time(attention2.deadline_at, attention.deadline_at)

        # Notification dedup survived the restart.
        assert await registry2.attention.already_notified(
            attention.id, "high"
        ) is True

        cursor3 = await registry2.imports.get_cursor(conn.id, "gmail_history")
        assert cursor3 is not None
        assert (cursor3.id, cursor3.connection_id, cursor3.kind,
                cursor3.cursor_value) == snapshot["cursor"]

        job2 = await registry2.imports.get_import_job(job.id)
        assert job2 is not None
        assert (job2.id, job2.connection_id, job2.kind,
                job2.state, job2.total) == snapshot["job"]

        await engine2.dispose()

    async def test_relay_seen_survives_restart(self, tmp_path: Path):
        """The ``relay_seen_messages`` dedup row is durable: after dispose →
        re-open, ``is_seen`` still reports the message as ingested, so a
        re-posted ``(device_id, message_id)`` stays a no-op. (workstream H.)"""
        db_path = tmp_path / "relay_seen.db"
        engine = await _make_engine(db_path, extra_tables=[RelaySeenMessageRow.__table__])
        registry = _build_registry(engine)

        conn = await registry.connections.create_connection(
            kind="relay_device",
            label="Apple Messages relay",
            config={"device_name": "Relay"},
            auth_status="authenticated",
            operational_status="never_synced",
            enabled=True,
        )
        device_id = "relay-abc123"
        await registry.relay_seen.mark_seen(
            conn.id, device_id, "imsg-42", content_ref="relay://relay-abc123/msg/d"
        )
        assert await registry.relay_seen.is_seen(conn.id, device_id, "imsg-42") is True
        assert await registry.relay_seen.count_seen(conn.id, device_id) == 1

        # Simulated restart: dispose and re-open a fresh engine + registry.
        await engine.dispose()
        engine2 = await _make_engine(db_path)
        registry2 = _build_registry(engine2)

        assert await registry2.relay_seen.is_seen(conn.id, device_id, "imsg-42") is True
        assert await registry2.relay_seen.count_seen(conn.id, device_id) == 1
        # Idempotent re-mark does not create a second row.
        await registry2.relay_seen.mark_seen(conn.id, device_id, "imsg-42")
        assert await registry2.relay_seen.count_seen(conn.id, device_id) == 1

        await engine2.dispose()

    async def test_wave_three_survives_restart(self, tmp_path: Path):
        """Bills, routines/sleep, wellness, and calendar records also
        survive the dispose → re-open cycle, read back through the same
        list/query methods the endpoints use."""
        db_path = tmp_path / "wave3_plane.db"
        engine = await _make_engine(db_path, extra_tables=WAVE3_TABLES)
        registry = _build_registry(engine)

        # Repositories exactly as the endpoint wiring builds them
        # (``backend.api.endpoints.{bills,routines,wellness}``).
        bills = BillsRepository(registry.session_factory)
        routines = RoutinesRepository(registry.session_factory)
        wellness = WellnessRepository(registry.session_factory)
        calendar = registry.calendar

        # -- bills + bill candidates (G) ------------------------------------
        rent = await bills.create_bill(
            payee="Downtown Properties LLC",
            amount_cents=14250,
            due_at=datetime(2026, 10, 31, 0, 0, tzinfo=UTC),
            currency="USD",
            recurrence="monthly",
            interval={"months": 1},
            evidence=[
                {"source_kind": "email", "source_id": "comm-1",
                 "snippet": "rent is due by the 31st", "confidence": 0.97},
            ],
        )
        utility = await bills.create_bill(
            payee="City Electric",
            amount_cents=8341,
            due_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
            recurrence="one-time",
        )
        # Explicit user confirm is the only paid writer — verify it sticks.
        paid_utility = await bills.mark_confirmed_paid(
            utility.id, at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        )
        assert paid_utility is not None
        assert paid_utility.status == "confirmed_paid"
        # The original ``utility`` object is stale after the separate
        # session commit — snapshot from the re-read.
        utility = paid_utility

        candidate = await bills.create_candidate(
            source_kind="email",
            source_id="comm-99",
            extracted_fields={
                "payee": {"value": "Downtown Properties LLC",
                          "confidence": 0.99},
                "amount_cents": {"value": 14250, "confidence": 0.95},
                "due_date": {"value": "2026-11-30T00:00:00+00:00",
                             "confidence": 0.9},
                "recurrence": {"value": "monthly", "confidence": 0.9},
                "interval": {"value": {"months": 1}},
            },
            evidence=[
                {"source_kind": "email", "source_id": "comm-99",
                 "snippet": "November rent statement", "confidence": 0.98},
            ],
            needs_review=["due_date"],
        )
        found = await bills.find_duplicate_candidate(
            payee="Downtown Properties LLC",
            amount_cents=14250,
            due_at=datetime(2026, 11, 30, 0, 0, tzinfo=UTC),
        )
        assert found is not None and found.id == candidate.id

        dup2 = await bills.create_candidate(
            source_kind="email",
            source_id="comm-100",
            extracted_fields={
                "payee": {"value": "downtown properties llc",
                          "confidence": 0.97},
                "amount_cents": {"value": 14251, "confidence": 0.95},
                "due_date": {"value": "2026-11-29T00:00:00+00:00",
                             "confidence": 0.9},
            },
            evidence=[
                {"source_kind": "email", "source_id": "comm-100",
                 "snippet": "November rent (again)", "confidence": 0.96},
            ],
        )
        dup2 = await bills.set_candidate_duplicate_of(dup2.id, found.id)
        assert dup2 is not None
        assert dup2.duplicate_of == found.id

        # Explicit accept promotes atomically: candidate → accepted + linked.
        accepted = await bills.accept_candidate(candidate.id)
        assert accepted is not None
        cand_after, bill_after = accepted
        assert cand_after.status == "accepted"
        assert cand_after.bill_id == bill_after.id
        assert bill_after.status == "pending"
        assert bill_after.payee == "Downtown Properties LLC"

        # -- routines + check-ins + sleep (H) --------------------------------
        wake = await routines.create_routine(
            kind="wake",
            title="Wake up",
            schedule={
                "times": ["07:30"],
                "weekdays": [],
                "timezone": "America/Los_Angeles",
            },
        )
        prep = await routines.create_routine(
            kind="workday_prep",
            title="Workday prep",
            schedule={
                "times": ["08:00"],
                "weekdays": [0, 1, 2, 3, 4],
                "timezone": "America/Los_Angeles",
            },
        )
        bed = await routines.create_routine(
            kind="bedtime",
            title="Bedtime wind-down",
            schedule={
                "times": ["22:00"],
                "weekdays": [5, 6, 0],
                "timezone": "America/Los_Angeles",
            },
        )
        await routines.update_routine(
            wake.id,
            deterministic_fallback_alarm={
                "at": "2026-10-28T07:30",
                "timezone": "America/Los_Angeles",
                "trigger_level": "normal",
                "basis": "clock",
            },
        )
        await routines.merge_missed_flags(
            wake.id, {"2026-10-26T07:30": 1}
        )
        wake = await routines.touch_completed(
            wake.id, datetime(2026, 10, 27, 7, 41, tzinfo=UTC)
        )
        assert wake is not None
        assert wake.last_completed_at is not None
        # Same-day re-check-in updates in place — one row, latest values.
        await routines.upsert_check_in(
            routine_id=prep.id,
            at=datetime(2026, 10, 26, 8, 2, tzinfo=UTC),
            completed=True,
            note="done early",
        )
        await routines.upsert_check_in(
            routine_id=prep.id,
            at=datetime(2026, 10, 26, 8, 5, tzinfo=UTC),
            completed=True,
            note="took 20 min",
        )
        # Same-day re-check-in updated the row in place: one row, and the
        # re-read shows the latest values (the returned row is a fresh
        # session object — snapshot from it, not the first call's object).
        checkins = await routines.list_check_ins(prep.id)
        assert len(checkins) == 1
        checkin = checkins[0]
        assert checkin.note == "took 20 min"
        # A disabled routine proves the enabled flag is durable.
        # (asserted post-restart via list_routines(enabled_only=True))
        await routines.update_routine(bed.id, enabled=False)
        sleep1 = await routines.create_sleep_record(
            bed_at=datetime(2026, 10, 26, 23, 30, tzinfo=UTC),
            wake_at=datetime(2026, 10, 27, 6, 45, tzinfo=UTC),
            source="manual",
        )
        assert sleep1.duration_minutes == 435
        # A partial record (bedtime only) stays unknown, not zero.
        sleep2 = await routines.create_sleep_record(
            bed_at=datetime(2026, 10, 27, 0, 10, tzinfo=UTC),
            source="apple_health",
            consent_granted=False,
        )
        assert sleep2.wake_at is None
        assert sleep2.duration_minutes is None

        # -- wellness entries + preferences (I) -----------------------------
        entry, created = await wellness.upsert_by_day(
            at=datetime(2026, 10, 26, 15, 0, tzinfo=UTC),
            mood=3,
            energy=6,
            journal_entry="long day",
        )
        assert created is True
        entry2, created2 = await wellness.upsert_by_day(
            at=datetime(2026, 10, 26, 15, 30, tzinfo=UTC),
            mood=4,
            sleep_minutes=435,
            sleep_consistency={"stdev_minutes": 34, "regularity": "stable"},
            energy=7,
            stress=2,
            journal_entry="slept well",
            observations={"steps": 9123},
        )
        # Same-day upsert updated in place, not a second row.
        assert created2 is False
        assert entry2.id == entry.id
        # The re-read carries the latest values (fresh session object).
        entry = await wellness.get_entry(entry2.id)
        assert entry is not None
        assert entry.mood == 4

        await wellness.set_preference(PREF_ENABLED, {"enabled": True})
        await wellness.log_export(
            entries=1, window="2026-09-27..2026-10-26", fmt="json"
        )

        # -- calendar accounts + events (E) ----------------------------------
        acct = await calendar.create_account(
            provider="google",
            label="Personal",
            account_ref="me@gmail.com",
            secret_ref="vault:google-oauth:me",
            timezone="America/Los_Angeles",
            config={"calendar_ids": ["primary"]},
        )
        await calendar.save_sync_state(
            acct.id,
            {"last_sync_at": "2026-10-26T04:00:00Z",
             "tokens": {"primary": "tok-123"}},
        )
        await calendar.mark_attempt(
            acct.id,
            operational_status="healthy",
            last_attempt_at=datetime(2026, 10, 27, 18, 0, tzinfo=UTC),
            last_success_at=datetime(2026, 10, 27, 18, 0, tzinfo=UTC),
        )
        # The original ``acct`` row is stale after the UPDATE commit —
        # snapshot from the re-read.
        acct = await calendar.get_account(acct.id)
        assert acct is not None
        assert acct.operational_status == "healthy"
        ev1 = await calendar.upsert_event(
            account_id=acct.id,
            ical_uid="evt-1",
            title="1:1 with Sam",
            start=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
            end=datetime(2026, 10, 28, 16, 0, tzinfo=UTC),
            location="Room 4B",
            timezone="America/Los_Angeles",
            changed_at="2026-10-20T10:00:00Z",
            extra={"calendar_id": "primary"},
        )
        ev2 = await calendar.upsert_event(
            account_id=acct.id,
            ical_uid="all-1",
            title="Dentist day",
            start=datetime(2026, 10, 30, 0, 0, tzinfo=UTC),
            all_day=True,
        )
        await calendar.upsert_event(
            account_id=acct.id,
            ical_uid="del-1",
            title="Old thing",
            start=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
            deleted=True,
        )

        # Snapshot the values that must be unchanged after the restart.
        snapshot = {
            "rent": (rent.id, rent.payee, rent.amount_cents, rent.currency,
                     rent.recurrence, rent.interval, rent.status,
                     rent.evidence, rent.duplicate_of),
            "utility": (utility.id, utility.payee, utility.amount_cents,
                        utility.recurrence, utility.status),
            "utility_confirmed": paid_utility.confirmed_at,
            # Snapshot from ``cand_after`` — the post-accept object, whose
            # status/bill_id the accept call flipped (the original
            # ``candidate`` row object is stale after the promotion).
            "candidate": (candidate.id, cand_after.status,
                          cand_after.bill_id, cand_after.needs_review),
            "dup2": (dup2.id, dup2.status, dup2.duplicate_of),
            "wake": (wake.id, wake.kind, wake.title, wake.schedule,
                     wake.enabled, wake.deterministic_fallback_alarm,
                     wake.missed_flags),
            "prep": (prep.id, prep.kind, prep.schedule),
            "checkin": (checkin.id, checkin.routine_id, checkin.completed,
                        checkin.note),
            "sleep1": (sleep1.id, sleep1.source, sleep1.duration_minutes,
                       sleep1.consent_granted),
            "sleep2": (sleep2.id, sleep2.source, sleep2.duration_minutes,
                       sleep2.consent_granted),
            "wellness": (entry.id, entry.mood, entry.sleep_minutes,
                         entry.sleep_consistency, entry.energy,
                         entry.stress, entry.journal_entry,
                         entry.observations),
            "wellness_at": entry.at,
            "acct": (acct.id, acct.provider, acct.label, acct.account_ref,
                     acct.secret_ref, acct.timezone, acct.config,
                     acct.sync_state, acct.enabled,
                     acct.operational_status),
            "ev1": (ev1.id, ev1.account_id, ev1.ical_uid, ev1.title,
                    ev1.all_day, ev1.location, ev1.timezone, ev1.changed_at,
                    ev1.appt, ev1.deleted, ev1.status, ev1.extra),
            "ev2": (ev2.id, ev2.ical_uid, ev2.title, ev2.all_day),
        }

        # -- Phase 2: simulated process restart (engine disposed). -----------
        await engine.dispose()

        # -- Phase 3: re-create engine + factory the way the app would. ------
        engine2 = await _make_engine(db_path, extra_tables=WAVE3_TABLES)
        registry2 = _build_registry(engine2)
        bills2 = BillsRepository(registry2.session_factory)
        routines2 = RoutinesRepository(registry2.session_factory)
        wellness2 = WellnessRepository(registry2.session_factory)
        calendar2 = registry2.calendar

        # -- bills + candidates ---------------------------------------------
        all_bills = await bills2.list_bills()
        assert {b.id for b in all_bills} == {rent.id, utility.id,
                                             bill_after.id}

        rent2 = await bills2.get_bill(rent.id)
        assert rent2 is not None
        assert (rent2.payee, rent2.amount_cents, rent2.currency,
                rent2.recurrence, rent2.interval, rent2.status,
                rent2.evidence, rent2.duplicate_of) == snapshot["rent"][1:]
        assert _same_time(rent2.due_at, rent.due_at)

        utility2 = await bills2.get_bill(utility.id)
        assert utility2 is not None
        assert utility2.status == "confirmed_paid"
        assert _same_time(utility2.confirmed_at,
                         snapshot["utility_confirmed"])

        promoted2 = await bills2.get_bill(bill_after.id)
        assert promoted2 is not None
        assert (promoted2.payee, promoted2.amount_cents,
                promoted2.recurrence, promoted2.interval,
                promoted2.status, promoted2.currency) == (
            "Downtown Properties LLC", "14250", "monthly",
            {"months": 1}, "pending", "USD")

        # The paid filter and window queries come back the same.
        paid = await bills2.list_bills(status="confirmed_paid")
        assert [b.id for b in paid] == [utility.id]
        upcoming = await bills2.upcoming(
            days=14, now=datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        )
        assert [b.id for b in upcoming] == [rent.id]
        assert [b.id for b in await bills2.monthly_calendar("2026-10")] \
            == [rent.id]

        cands = await bills2.list_candidates()
        assert {c.id for c in cands} == {candidate.id, dup2.id}
        accepted2 = await bills2.get_candidate(candidate.id)
        assert accepted2 is not None
        assert (accepted2.status, accepted2.bill_id,
                accepted2.needs_review) == snapshot["candidate"][1:]
        dup2r = await bills2.get_candidate(dup2.id)
        assert dup2r is not None
        assert (dup2r.status, dup2r.duplicate_of) == snapshot["dup2"][1:]

        # -- routines + check-ins + sleep ------------------------------------
        all_routines = await routines2.list_routines()
        assert {r.id for r in all_routines} == {wake.id, prep.id, bed.id}
        # The disabled flag survived: only two are enabled.
        enabled = await routines2.list_routines(enabled_only=True)
        assert {r.id for r in enabled} == {wake.id, prep.id}

        wake2 = await routines2.get_routine(wake.id)
        assert wake2 is not None
        assert (wake2.kind, wake2.title, wake2.schedule, wake2.enabled,
                wake2.deterministic_fallback_alarm, wake2.missed_flags) \
            == snapshot["wake"][1:]
        assert _same_time(wake2.last_completed_at, wake.last_completed_at)
        assert wake2.missed_flags == {"2026-10-26T07:30": 1}

        prep2 = await routines2.get_routine(prep.id)
        assert prep2 is not None
        assert (prep2.kind, prep2.schedule) == snapshot["prep"][1:]

        # Weekday filter over the durable schedule (2026-10-27 is a
        # Tuesday → weekday 1: wake [] and workdays [0..4] are due,
        # the weekend-only bedtime routine is not).
        due = await routines2.routines_due_on_local_day(
            date(2026, 10, 27), "America/Los_Angeles"
        )
        assert {r.id for r in due} == {wake.id, prep.id}

        checkins = await routines2.list_check_ins(prep.id)
        assert len(checkins) == 1
        ci2 = checkins[0]
        assert (ci2.routine_id, ci2.completed, ci2.note) \
            == snapshot["checkin"][1:]
        assert _same_time(ci2.at, checkin.at)

        sleeps = await routines2.list_sleep_records(
            days=14, now=datetime(2026, 10, 27, 20, 0, tzinfo=UTC)
        )
        assert {s.id for s in sleeps} == {sleep1.id, sleep2.id}
        latest = await routines2.latest_sleep_record()
        assert latest is not None and latest.id == sleep2.id
        assert latest.source == "apple_health"
        assert latest.duration_minutes is None

        # The partial record kept its NULL wake_at and unknown duration.
        sleep1r = next(s for s in sleeps if s.id == sleep1.id)
        assert (sleep1r.source, sleep1r.duration_minutes,
                sleep1r.consent_granted) == snapshot["sleep1"][1:]
        assert _same_time(sleep1r.bed_at, sleep1.bed_at)
        assert _same_time(sleep1r.wake_at, sleep1.wake_at)
        sleep2r = next(s for s in sleeps if s.id == sleep2.id)
        assert sleep2r.wake_at is None
        assert (sleep2r.source, sleep2r.duration_minutes,
                sleep2r.consent_granted) == snapshot["sleep2"][1:]

        # -- wellness + preferences ------------------------------------------
        entries = await wellness2.list_entries()
        assert len(entries) == 1
        entry2 = entries[0]
        assert (entry2.mood, entry2.sleep_minutes,
                entry2.sleep_consistency, entry2.energy, entry2.stress,
                entry2.journal_entry, entry2.observations) \
            == snapshot["wellness"][1:]
        assert _same_time(entry2.at, entry.at)

        assert await wellness2.get_preference(PREF_ENABLED) == {
            "enabled": True
        }
        export_log = await wellness2.get_preference(EXPORT_LOG_KEY)
        assert export_log is not None
        assert len(export_log) == 1
        assert export_log[0]["code"] == "wellness_export"
        assert export_log[0]["entries"] == 1
        assert export_log[0]["format"] == "json"

        # -- calendar accounts + events ---------------------------------------
        accounts = await calendar2.list_accounts()
        assert len(accounts) == 1
        acct2 = accounts[0]
        assert (acct2.provider, acct2.label, acct2.account_ref,
                acct2.secret_ref, acct2.timezone, acct2.config,
                acct2.sync_state, acct2.enabled, acct2.operational_status) \
            == snapshot["acct"][1:]
        assert _same_time(acct2.last_attempt_at,
                         datetime(2026, 10, 27, 18, 0, tzinfo=UTC))
        assert _same_time(acct2.last_success_at,
                         datetime(2026, 10, 27, 18, 0, tzinfo=UTC))

        window = await calendar2.list_events(
            account_id=acct.id,
            start=datetime(2026, 10, 27, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 4, 0, 0, tzinfo=UTC),
        )
        assert {e.ical_uid for e in window} == {"evt-1", "all-1"}

        ev1r = next(e for e in window if e.ical_uid == "evt-1")
        assert (ev1r.account_id, ev1r.ical_uid, ev1r.title,
                ev1r.all_day, ev1r.location, ev1r.timezone,
                ev1r.changed_at, ev1r.appt, ev1r.deleted, ev1r.status,
                ev1r.extra) == snapshot["ev1"][1:]
        assert _same_time(ev1r.start, ev1.start)
        assert _same_time(ev1r.end, ev1.end)

        ev2r = next(e for e in window if e.ical_uid == "all-1")
        assert (ev2r.ical_uid, ev2r.title, ev2r.all_day) \
            == snapshot["ev2"][1:]
        assert ev2r.title == "Dentist day"

        # Deleted events stay durable — excluded by default, visible with
        # ``include_deleted`` (freeze §8: never silently drop).
        with_deleted = await calendar2.list_events(
            account_id=acct.id,
            start=datetime(2026, 10, 27, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 4, 0, 0, tzinfo=UTC),
            include_deleted=True,
        )
        assert {e.ical_uid for e in with_deleted} == \
            {"evt-1", "all-1", "del-1"}

        await engine2.dispose()

    async def test_wiring_builds_registry_from_engine(self, tmp_path: Path):
        """The exact wiring path (make_session_factory → RepositoryRegistry)
        yields a working registry, proving the Wave-1 contract is callable."""
        engine = await _make_engine(tmp_path / "wiring.db")
        registry = _build_registry(engine)
        assert registry is not None
        assert registry.session_factory is not None
        # The registry exposes every per-plane repository.
        for name in ("connections", "communications", "commitments",
                     "attention", "imports"):
            assert getattr(registry, name) is not None
        await engine.dispose()

    async def test_secret_roundtrip_with_custom_codec(self, tmp_path: Path):
        """A real (non-identity) codec round-trips through the durable layer."""

        class PrefixCodec:
            def encrypt_secret(self, plaintext: str) -> dict[str, object]:
                return {"c": f"enc:{plaintext}", "v": 1}

            def decrypt_secret(self, payload: dict[str, object]) -> str:
                return str(payload["c"]).removeprefix("enc:")

        engine = await _make_engine(tmp_path / "codec.db")
        from backend.persistence.connection import make_session_factory

        registry = RepositoryRegistry(
            make_session_factory(engine), secret_codec=PrefixCodec()
        )
        conn = await registry.connections.create_connection(kind="imap")
        await registry.connections.upsert_secret(
            kind="token", owner_kind="connection", owner_id=conn.id,
            plaintext="raw-token-value",
        )
        # The stored payload must be the codec ciphertext, not plaintext.
        # (SQLite stores JSONB as TEXT, so read it back as a raw string and
        # parse it — the plaintext must not appear anywhere in it.)
        async with make_session_factory(engine)() as session:
            row = (await session.execute(
                text(
                    "SELECT payload FROM secrets WHERE owner_id = :oid"
                ),
                {"oid": conn.id},
            )).fetchone()
            raw_payload = row[0] if row else None
            assert raw_payload is not None
            import json
            payload = json.loads(raw_payload)
            # The stored payload must be exactly the codec's ciphertext
            # structure — the repository never stores plaintext.
            assert payload == {"c": "enc:raw-token-value", "v": 1}

        assert await registry.connections.get_secret(
            kind="token", owner_kind="connection", owner_id=conn.id
        ) == "raw-token-value"
        await engine.dispose()


class TestSchema:
    """The 14 frozen tables exist and match the 006 migration's table set."""

    def test_migration_head_is_linear_after_005(self):
        m = importlib.import_module(
            "backend.persistence.alembic.versions.006_personal_plane"
        )
        assert m.revision == "006_personal_plane"
        assert m.down_revision == "005_nc_platform"

    def test_orm_tables_match_migration_create_table_set(self):
        """The ORM registers exactly the 14 tables the 006 migration creates."""
        import ast

        m = importlib.import_module(
            "backend.persistence.alembic.versions.006_personal_plane"
        )
        src = Path(m.__file__).read_text()
        tree = ast.parse(src)
        created: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "create_table":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        created.add(str(node.args[0].value))
        assert created == set(PERSONAL_TABLE_NAMES), (
            f"migration table set mismatch:\n"
            f"  migration-only: {sorted(created - set(PERSONAL_TABLE_NAMES))}\n"
            f"  orm-only:      {sorted(set(PERSONAL_TABLE_NAMES) - created)}"
        )
        assert m.revision == "006_personal_plane"

    async def test_tables_exist_after_create_all(self, tmp_path: Path):
        """After ``create_all`` on a fresh DB, all 14 tables are present."""
        engine = await _make_engine(tmp_path / "schema.db")
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            present = {row[0] for row in result.fetchall()}
        assert set(PERSONAL_TABLE_NAMES) <= present, (
            f"missing tables: {set(PERSONAL_TABLE_NAMES) - present}"
        )
        await engine.dispose()


class TestRepositoryRoundTrip:
    """Every one of the 14 frozen tables is written and read back through
    the repository layer on a fresh engine — the full plane in one pass."""

    async def test_all_fourteen_tables_roundtrip(self, tmp_path: Path):
        engine = await _make_engine(tmp_path / "roundtrip.db")
        registry = _build_registry(engine)

        # connections + secrets (2)
        conn = await registry.connections.create_connection(
            kind="imessage", label="iMessage Relay", config={"relay": "mac1"}
        )
        await registry.connections.upsert_secret(
            kind="pairing_token", owner_kind="connection",
            owner_id=conn.id, plaintext="pairing-secret",
        )
        assert await registry.connections.get_secret(
            kind="pairing_token", owner_kind="connection", owner_id=conn.id
        ) == "pairing-secret"

        # people + person_identities (2)
        person = await registry.communications.create_person(
            display_name="Grace Hopper", vip=True,
        )
        identity = await registry.communications.upsert_person_identity(
            person_id=person.id, channel="imessage", handle="+14155550000",
            resolved=True,
        )
        assert identity.resolved is True

        # communications + conversations (2)
        comm = await registry.communications.create_communication(
            person_id=person.id, channel="imessage", direction="incoming",
            content_ref="content://imessage/msg-1",
            received_at=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        )
        await registry.communications.upsert_conversation(
            person_id=person.id, channel="imessage", unread_count=2,
        )

        # commitments + followups (2)
        commit = await registry.commitments.create_commitment(
            person_id=person.id, text="Review Grace's PR", state="open",
        )
        await registry.commitments.set_commitment_state(commit.id, "done")
        followup = await registry.commitments.create_followup(
            text="Send thanks note", person_id=person.id,
            linked_refs=[comm.id],
        )

        # attention_items + snoozes + notification_log (3)
        item = await registry.attention.create_attention_item(
            title="Review PR", importance=7, urgency="normal",
        )
        await registry.attention.snooze(
            item.id, until=datetime(2026, 10, 12, 0, 0, tzinfo=UTC),
            reason="out of office",
        )
        await registry.attention.record_notification(item.id, level="normal")
        assert await registry.attention.already_notified(item.id, "normal")
        assert not await registry.attention.already_notified(item.id, "critical")

        # preferences (1) — no repo yet; use the session factory directly.
        from sqlalchemy import select

        async with registry.session_factory() as session:
            session.add(PreferenceRow(key="ui.theme", value={"mode": "dark"}))
            await session.commit()
        async with registry.session_factory() as session:
            pref = (await session.execute(
                select(PreferenceRow).where(PreferenceRow.key == "ui.theme")
            )).scalar_one()
            assert pref.value == {"mode": "dark"}

        # ingest_cursors + import_jobs (2)
        await registry.imports.set_cursor(conn.id, "relay_seq", {"seq": 7})
        job = await registry.imports.create_import_job(conn.id, "messages")
        await registry.imports.finish_import_job(job.id, state="done")
        job2 = await registry.imports.get_import_job(job.id)
        assert job2.state == "done"
        assert job2.finished_at is not None

        await engine.dispose()
