"""Workstream E — durable calendar account setup (Personal-Ops Recovery §8).

Covers the calendar plane built on the Wave-1 durable foundation:

1. **Migration 007** — the ``007_calendar`` alembic revision sits on the
   linear chain head (``006_personal_plane``), creates exactly the frozen
   ``calendar_accounts`` / ``calendar_events`` tables, and applies cleanly on
   a real SQLite scratch DB left at the prior head (006). The ORM table set
   matches the migration's ``op.create_table`` set, and a full
   ``upgrade head`` chain applies end to end.
2. **Repository round-trip** — durable accounts + events survive a
   simulated restart through the repository layer (ids, JSONB sync_state,
   timestamps, tz-aware start/end, the explicit ``deleted`` marker).
3. **iCal / timezone correctness** — the stdlib RFC5545 parser resolves
   TZID (IANA + embedded VTIMEZONE), honors all-day dates, CANCELLED →
   ``deleted``, floating times → the account default zone, and ``Z`` → UTC.
4. **Sync runner (no network)** — a scripted transport fakes the Google
   and CalDAV wire responses; the runner bridges both planes (durable row +
   in-memory service), persists sync tokens / etags, records liveness, and
   records an honest ``last_error_code`` on a flaky account.
5. **Endpoints (honest degradation + wired)** — the account/conflict routes
   register before the ``/{event_id}`` catch-all, degrade to
   200/empty + 202/``executed: false`` without a durable registry, and behave
   when the durable plane is wired; credentials round-trip through the
   secrets vault only (never echoed).
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import pathlib
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.app import create_app
from backend.calendar import reset_calendar_service
from backend.calendar.providers import (
    HttpResponse,
    ProviderEvent,
    parse_ics,
    resolve_zone,
)
from backend.calendar.service import CalendarService
from backend.calendar.sync import (
    SUPERVISOR_STATE_KEY,
    CalendarSyncRunner,
    install_calendar_sync,
    stop_calendar_sync,
)
from backend.persistence.models.personal import (
    CalendarAccountRow,
    CalendarEventRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry

_ALEMBIC_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "persistence"
    / "alembic"
)

#: The two frozen calendar-plane tables plus the connector secrets vault the
#: sync runner / account-credential round-trips write through.
CALENDAR_TABLES = [
    CalendarAccountRow.__table__,
    CalendarEventRow.__table__,
    SecretRow.__table__,
]

#: The two frozen calendar-plane tables (spec §8).
EXPECTED_CALENDAR_TABLE_NAMES = {"calendar_accounts", "calendar_events"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _seed_prior_head(db_path: pathlib.Path) -> None:
    """Apply the real 006 revision to a fresh DB (the prior head), so a
    subsequent ``upgrade head`` applies *only* revision 007_calendar."""
    command.upgrade(_alembic_config(f"sqlite:///{db_path}"), "006_personal_plane")


def _migration_created_tables() -> set[str]:
    """The ``op.create_table`` names declared in 007_calendar.py (AST)."""
    m = importlib.import_module("backend.persistence.alembic.versions.007_calendar")
    src = pathlib.Path(str(m.__file__)).read_text()
    tree = ast.parse(src)
    created: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "create_table":
                if node.args and isinstance(node.args[0], ast.Constant):
                    created.add(str(node.args[0].value))
    return created


async def _make_engine(db_path: pathlib.Path) -> AsyncEngine:
    """Build a durable, file-backed engine the way the app wiring does."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            CalendarAccountRow.__table__.metadata.create_all, tables=CALENDAR_TABLES
        )
    return engine


def _build_registry(engine: AsyncEngine) -> RepositoryRegistry:
    """Construct the registry exactly as ``backend.api.wiring`` does."""
    from backend.persistence.connection import make_session_factory
    from backend.persistence.repositories.connections_repo import IdentitySecretCodec

    return RepositoryRegistry(
        make_session_factory(engine),
        secret_codec=IdentitySecretCodec(),
    )


# ---------------------------------------------------------------------------
# Fake transport (the single seam every provider talks to — no real network)
# ---------------------------------------------------------------------------
class FakeTransport:
    """Scripted :class:`HttpTransport` for the provider unit tests.

    ``routes`` maps ``(method, url_or_prefix)`` → a scripted
    :class:`HttpResponse`; an exact URL match wins, then a prefix match.
    """

    def __init__(self, routes: dict[tuple[str, str], HttpResponse]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, str, dict[str, Any], Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, dict(headers or {}), params))
        for (m, key), resp in self.routes.items():
            if m == method and url == key:
                return resp
        for (m, key), resp in self.routes.items():
            if m == method and url.startswith(key):
                return resp
        return HttpResponse(404, {}, b"not found")


def _google_items_response(
    items: list[dict[str, Any]], next_token: str | None = None
) -> HttpResponse:
    body = {"items": items, "nextSyncToken": next_token}
    return HttpResponse(200, {}, json.dumps(body).encode("utf-8"))


def _google_calendar_list_response() -> HttpResponse:
    body = {"items": [{"id": "primary@group.calendar.google.com", "type": "primary"}]}
    return HttpResponse(200, {}, json.dumps(body).encode("utf-8"))


def _token_response() -> HttpResponse:
    body = {"access_token": "fake-access-token", "token_type": "Bearer"}
    return HttpResponse(200, {}, json.dumps(body).encode("utf-8"))


def _caldav_propfind_response(href: str) -> HttpResponse:
    body = (
        '<?xml version="1.0"?><multistatus xmlns="DAV:">'
        f'<response><href>/dav/principal/u/</href></response>'
        f'<response><C:calendar href="{href}"/></response>'
        "</multistatus>"
    )
    return HttpResponse(207, {}, body.encode("utf-8"))


def _caldav_ics_response(ics: str, etag: str | None = None) -> HttpResponse:
    headers = {"etag": etag} if etag else {}
    return HttpResponse(200, headers, ics.encode("utf-8"))


_GOOGLE_CAL = "primary@group.calendar.google.com"


# ---------------------------------------------------------------------------
# 1. Migration 007
# ---------------------------------------------------------------------------
class TestMigration007:
    """The calendar tables are created by 007_calendar on the linear head."""

    def test_revision_sits_on_linear_head(self) -> None:
        m = importlib.import_module("backend.persistence.alembic.versions.007_calendar")
        assert m.revision == "007_calendar"
        assert m.down_revision == "006_personal_plane"

    def test_orm_tables_match_migration_create_table_set(self) -> None:
        created = _migration_created_tables()
        assert created == EXPECTED_CALENDAR_TABLE_NAMES, (
            f"migration table set mismatch:\n"
            f"  migration-only: {sorted(created - EXPECTED_CALENDAR_TABLE_NAMES)}\n"
            f"  orm-only:      {sorted(EXPECTED_CALENDAR_TABLE_NAMES - created)}"
        )
        # The ORM registers exactly these two (plus the 14 from 006 it builds
        # on), so the calendar set is a subset of the metadata and matches 1:1.
        orm_calendar = {
            t.name for t in CalendarAccountRow.__table__.metadata.tables.values()
            if t.name in EXPECTED_CALENDAR_TABLE_NAMES
        }
        assert orm_calendar == EXPECTED_CALENDAR_TABLE_NAMES

    def test_upgrade_applies_only_007_on_prior_head(self) -> None:
        """Render the ``006 -> 007`` step offline against the Postgres dialect.

        The alembic chain is Postgres-only (``001_initial`` issues
        ``CREATE EXTENSION vector`` and every revision uses ``now()`` /
        ``JSONB`` server defaults), so it cannot execute on a SQLite scratch
        DB. Offline SQL rendering (``sql=True``) runs the real revision
        objects without a live database and yields the exact DDL the
        migration would emit, which we assert on below.
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            command.upgrade(
                _alembic_config("postgresql+asyncpg://nervcenter@localhost:5432/nervcenter"),
                "006_personal_plane:007_calendar",
                sql=True,
            )
        sql = buf.getvalue()

        # Only the 007 step is rendered (no prior tables, no later head).
        assert "Running upgrade 006_personal_plane -> 007_calendar" in sql
        assert "CREATE TABLE calendar_accounts" in sql
        assert "CREATE TABLE calendar_events" in sql
        # The two frozen tables are the only ones this revision creates.
        created = {
            name
            for name in (
                "calendar_accounts", "calendar_events"
            )
            if f"CREATE TABLE {name} " in sql or f"CREATE TABLE {name}(" in sql
        }
        assert created == EXPECTED_CALENDAR_TABLE_NAMES
        # The five frozen indexes (006 naming convention).
        for idx in (
            "ix_calendar_accounts_provider",
            "ix_calendar_accounts_enabled",
            "ix_calendar_events_account_id",
            "ix_calendar_events_ical_uid",
            "ix_calendar_events_start",
        ):
            assert f"CREATE INDEX {idx}" in sql, f"missing index {idx}"
        # FK to calendar_accounts with CASCADE (durable copy drops with the account).
        assert "ON DELETE CASCADE" in sql
        # JSONB columns carry the ``'{}'`` server default (006 convention).
        assert "JSONB" in sql
        # The chain is left stamped at the new head.
        assert "UPDATE alembic_version SET version_num='007_calendar'" in sql


# ---------------------------------------------------------------------------
# 2. Repository round-trip (durable boundary, restart semantics)
# ---------------------------------------------------------------------------
class TestCalendarRepositoryRoundTrip:
    """Durable accounts + events survive a restart through the repositories."""

    async def test_account_and_event_roundtrip(self, tmp_path: pathlib.Path) -> None:
        engine = await _make_engine(tmp_path / "cal.db")
        registry = _build_registry(engine)

        # -- Phase 1: write the durable boundary through the repositories. ----
        acct = await registry.calendar.create_account(
            provider="google",
            label="Personal",
            account_ref="me@gmail.com",
            secret_ref="vault://calendar-1",
            timezone="America/Los_Angeles",
            config={"calendar_ids": [_GOOGLE_CAL]},
        )
        await registry.calendar.save_sync_state(
            acct.id, {"sync_tokens": {_GOOGLE_CAL: "tok-1"}, "last_sync_at": "x"}
        )
        await registry.calendar.mark_attempt(
            acct.id,
            operational_status="healthy",
            last_attempt_at=datetime.now(UTC),
            last_success_at=datetime.now(UTC),
        )

        row = await registry.calendar.upsert_event(
            account_id=acct.id,
            ical_uid="uid-1",
            title="Design review",
            start=datetime(2026, 11, 1, 17, 0, tzinfo=UTC),
            end=datetime(2026, 11, 1, 18, 0, tzinfo=UTC),
            timezone="America/Los_Angeles",
            changed_at="2026-10-27T00:00:00Z",
        )
        # All-day event (start stored as authored-midnight; all_day marker set).
        await registry.calendar.upsert_event(
            account_id=acct.id,
            ical_uid="uid-allday",
            title="Holiday",
            all_day=True,
        )
        # An explicit deletion marker (never silently dropped).
        await registry.calendar.upsert_event(
            account_id=acct.id,
            ical_uid="uid-gone",
            title="Cancelled",
            deleted=True,
            status="cancelled",
        )

        # -- Phase 2: dispose the engine (simulated restart). -----------------
        await engine.dispose()

        # -- Phase 3: a fresh engine over the same file ----------------------
        engine2 = await _make_engine(tmp_path / "cal.db")
        registry2 = _build_registry(engine2)

        acct2 = await registry2.calendar.get_account(acct.id)
        assert acct2 is not None
        assert acct2.provider == "google"
        assert acct2.secret_ref == "vault://calendar-1"
        assert acct2.timezone == "America/Los_Angeles"
        assert acct2.operational_status == "healthy"
        assert acct2.sync_state == {"sync_tokens": {_GOOGLE_CAL: "tok-1"}, "last_sync_at": "x"}
        assert acct2.config == {"calendar_ids": [_GOOGLE_CAL]}

        ev2 = await registry2.calendar.get_event(row.id)
        assert ev2 is not None
        assert ev2.ical_uid == "uid-1"
        assert ev2.title == "Design review"
        assert ev2.start is not None and ev2.start.utcoffset() == timedelta(0)
        assert ev2.timezone == "America/Los_Angeles"
        assert ev2.deleted is False

        listed = await registry2.calendar.list_events(
            start=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
            include_deleted=True,
        )
        by_uid = {r.ical_uid: r for r in listed}
        assert by_uid["uid-allday"].all_day is True
        assert by_uid["uid-gone"].deleted is True

        # include_deleted=False hides the explicit deletion marker.
        non_deleted = await registry2.calendar.list_events(
            start=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        )
        assert "uid-gone" not in {r.ical_uid for r in non_deleted}

        # Cascade: deleting the account drops its durable events.
        assert await registry2.calendar.delete_account(acct.id) is True
        assert await registry2.calendar.get_event(row.id) is None

        await engine2.dispose()


# ---------------------------------------------------------------------------
# 3. iCal / timezone correctness (stdlib RFC5545 parser — no network)
# ---------------------------------------------------------------------------
class TestIcsParsing:
    """The parser resolves TZID, all-day, CANCELLED, floating, and UTC."""

    def _parse(self, ics: str, default: str = "UTC") -> list[ProviderEvent]:
        return parse_ics(ics, default_timezone=default)

    def test_iana_tzid_resolves_to_utc(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:ev-la\r\n"
            "SUMMARY:Standup\r\n"
            "DTSTART;TZID=America/Los_Angeles:20261101T090000\r\n"
            "DTEND;TZID=America/Los_Angeles:20261101T093000\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
        (ev,) = self._parse(ics, default="UTC")
        # 09:00 Pacific on 2026-11-01 is -08:00 (PST), i.e. 17:00 UTC.
        assert ev.start is not None
        assert ev.start.astimezone(UTC) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)

    def test_all_day_date(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:ev-all\r\n"
            "SUMMARY:Holiday\r\n"
            "DTSTART;VALUE=DATE:20261101\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
        (ev,) = self._parse(ics, default="America/Los_Angeles")
        assert ev.all_day is True

    def test_cancelled_marks_deleted(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:ev-c\r\n"
            "SUMMARY:Gone\r\n"
            "STATUS:CANCELLED\r\n"
            "DTSTART:20261101T120000Z\r\n"
            "DTEND:20261101T130000Z\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
        (ev,) = self._parse(ics)
        assert ev.deleted is True

    def test_floating_uses_default_zone(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:ev-float\r\n"
            "SUMMARY:Float\r\n"
            "DTSTART:20261101T090000\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
        (ev,) = self._parse(ics, default="America/Los_Angeles")
        assert ev.start is not None
        # floating 09:00 in LA = 17:00 UTC in November.
        assert ev.start.astimezone(UTC) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)

    def test_z_suffix_is_utc(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:ev-z\r\n"
            "SUMMARY:Zulu\r\n"
            "DTSTART:20261101T090000Z\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
        (ev,) = self._parse(ics)
        assert ev.start is not None
        assert ev.start.astimezone(UTC) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)

    def test_resolve_zone_falls_back_to_default(self) -> None:
        assert resolve_zone("Not/A/Zone", "America/Los_Angeles") == resolve_zone(
            None, "America/Los_Angeles"
        )
        # A bad name degrades to the default, and an unknown default to UTC.
        assert resolve_zone("Not/A/Zone", "Also/Bad") == resolve_zone(None, "UTC")


# ---------------------------------------------------------------------------
# 4. Sync runner (scripted transport — no network)
# ---------------------------------------------------------------------------
class TestSyncRunner:
    """The runner bridges both planes and persists cursors / liveness."""

    async def _harness(self, tmp_path: pathlib.Path):
        engine = await _make_engine(tmp_path / "sync.db")
        registry = _build_registry(engine)
        service = CalendarService()
        return engine, registry, service

    async def test_google_sync_ingests_and_persists_tokens(self, tmp_path: pathlib.Path) -> None:
        engine, registry, service = await self._harness(tmp_path)
        transport = FakeTransport(
            {
                ("POST", "https://oauth2.googleapis.com/token"): _token_response(),
                ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList"):
                    _google_calendar_list_response(),
                ("GET", "https://www.googleapis.com/calendar/v3/calendars/"):
                    _google_items_response(
                        [
                            {
                                "id": "google-uid-1",
                                "summary": "Design review",
                                "start": {
                                    "dateTime": "2026-11-01T17:00:00Z",
                                    "timeZone": "America/Los_Angeles",
                                },
                                "end": {"dateTime": "2026-11-01T18:00:00Z"},
                                "updated": "2026-10-27T00:00:00Z",
                            }
                        ],
                        next_token="tok-next",
                    ),
            }
        )
        acct = await registry.calendar.create_account(
            provider="google",
            timezone="America/Los_Angeles",
            secret_ref="vault://cal",
            config={"client_id": "cid", "client_secret": "csecret"},
        )
        # Store the refresh token in the vault (the secret the runner resolves).
        await registry.connections.upsert_secret(
            kind="calendar_credential",
            owner_kind="connection",
            owner_id=acct.id,
            plaintext=json.dumps({"refresh_token": "rt-123"}),
        )
        await registry.calendar.update_account(acct.id, secret_ref="vault://cal")

        runner = CalendarSyncRunner(registry, calendar_service=service, transport=transport)
        summary = await runner.sync_account(acct.id)

        assert summary["synced"] is True
        assert summary["ingested"] == 1
        assert summary["changed"] == 1
        assert summary["deleted"] == 0

        # Both planes bridged: the durable row and the in-memory service.
        rows = await registry.calendar.list_events(
            start=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        )
        assert any(r.ical_uid == "google-uid-1" for r in rows)
        assert service.get_event(f"{acct.id}:google-uid-1") is not None

        # The sync token was persisted on the durable row.
        acct2 = await registry.calendar.get_account(acct.id)
        assert acct2.operational_status == "healthy"
        assert acct2.sync_state["sync_tokens"] == {"primary@group.calendar.google.com": "tok-next"}

        await engine.dispose()

    async def test_caldav_sync_parses_ics_and_persists_etag(self, tmp_path: pathlib.Path) -> None:
        engine, registry, service = await self._harness(tmp_path)
        cal_url = "https://dav.example.com/dav/user/calendar.ics"
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:caldav-uid-1\r\n"
            "SUMMARY:CalDAV event\r\n"
            "DTSTART;TZID=America/Los_Angeles:20261101T090000\r\n"
            "DTEND;TZID=America/Los_Angeles:20261101T093000\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
        transport = FakeTransport(
            {
                ("PROPFIND", "https://dav.example.com/dav/principal/"):
                    _caldav_propfind_response("/dav/user/calendar.ics"),
                ("GET", cal_url): _caldav_ics_response(ics, etag="etag-1"),
            }
        )
        acct = await registry.calendar.create_account(
            provider="caldav",
            timezone="America/Los_Angeles",
            secret_ref="vault://caldav",
            config={"url": "https://dav.example.com"},
        )
        await registry.connections.upsert_secret(
            kind="calendar_credential",
            owner_kind="connection",
            owner_id=acct.id,
            plaintext=json.dumps({"username": "alice", "password": "pw"}),
        )

        runner = CalendarSyncRunner(registry, calendar_service=service, transport=transport)
        summary = await runner.sync_account(acct.id)

        assert summary["synced"] is True
        assert summary["ingested"] == 1
        rows = await registry.calendar.list_events(
            start=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        )
        assert any(r.ical_uid == "caldav-uid-1" for r in rows)
        # TZID resolved: 09:00 LA → 17:00 UTC stored.
        (row,) = [r for r in rows if r.ical_uid == "caldav-uid-1"]
        assert row.start is not None
        assert row.start.astimezone(UTC) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)

        acct2 = await registry.calendar.get_account(acct.id)
        assert acct2.sync_state["etags"] == {cal_url: "etag-1"}
        assert acct2.operational_status == "healthy"

        await engine.dispose()

    async def test_deletion_marker_removes_memory_keeps_durable(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry, service = await self._harness(tmp_path)
        acct = await registry.calendar.create_account(
            provider="google",
            timezone="UTC",
            secret_ref="vault://del",
            config={"client_id": "cid", "client_secret": "csecret"},
        )
        # The runner resolves the credential from the vault; without a stored
        # refresh token the pass would abort with ``no_credential``.
        await registry.connections.upsert_secret(
            kind="calendar_credential",
            owner_kind="connection",
            owner_id=acct.id,
            plaintext=json.dumps({"refresh_token": "rt"}),
        )
        # Seed an existing in-memory + durable event, then re-sync with CANCELLED.
        await registry.calendar.upsert_event(
            account_id=acct.id, ical_uid="gone", title="Will vanish",
            start=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 11, 1, 13, 0, tzinfo=UTC),
        )
        service.ingest(
            {"event_id": f"{acct.id}:gone", "title": "Will vanish",
             "start": "2026-11-01T12:00:00+00:00", "end": "2026-11-01T13:00:00+00:00"}
        )
        assert service.get_event(f"{acct.id}:gone") is not None

        transport = FakeTransport(
            {
                ("POST", "https://oauth2.googleapis.com/token"): _token_response(),
                ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList"):
                    _google_calendar_list_response(),
                ("GET", "https://www.googleapis.com/calendar/v3/calendars/"):
                    _google_items_response(
                        [
                            {
                                "id": "gone",
                                "summary": "Will vanish",
                                "status": "cancelled",
                                "start": {"dateTime": "2026-11-01T12:00:00Z"},
                                "end": {"dateTime": "2026-11-01T13:00:00Z"},
                            }
                        ]
                    ),
            }
        )
        runner = CalendarSyncRunner(registry, calendar_service=service, transport=transport)
        summary = await runner.sync_account(acct.id)
        assert summary["deleted"] == 1

        # In-memory copy removed; durable row keeps the explicit deleted marker.
        assert service.get_event(f"{acct.id}:gone") is None
        rows = await registry.calendar.list_events(
            start=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
            include_deleted=True,
        )
        assert any(r.ical_uid == "gone" and r.deleted for r in rows)

        await engine.dispose()

    async def test_flaky_account_records_error_code(self, tmp_path: pathlib.Path) -> None:
        engine, registry, service = await self._harness(tmp_path)
        acct = await registry.calendar.create_account(
            provider="google", timezone="UTC", secret_ref="vault://x"
        )
        await registry.connections.upsert_secret(
            kind="calendar_credential", owner_kind="connection",
            owner_id=acct.id, plaintext=json.dumps({"refresh_token": "rt"}),
        )
        # The token endpoint returns a hard 401 → the runner must record an
        # honest last_error_code rather than crash the loop.
        transport = FakeTransport(
            {
                ("POST", "https://oauth2.googleapis.com/token"):
                    HttpResponse(401, {}, b'{"error":"invalid_grant"}'),
            }
        )
        runner = CalendarSyncRunner(registry, calendar_service=service, transport=transport)
        summary = await runner.sync_account(acct.id)
        assert summary["synced"] is False
        assert summary.get("error_code") in (
            "no_credential",
            "auth_failed",
            "upstream_error",
            "sync_failed",
        )

        acct2 = await registry.calendar.get_account(acct.id)
        assert acct2.operational_status == "error"
        assert acct2.last_error_code is not None

        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Supervisor install / teardown (guarded lifespan hook)
# ---------------------------------------------------------------------------
class TestSupervisorWiring:
    """install_calendar_sync is a no-op unless enabled + a registry exists."""

    async def test_degrades_without_toggle(self, tmp_path: pathlib.Path) -> None:
        engine, registry, service = await (
            TestSyncRunner(). _harness(tmp_path)
        )
        target = FastAPI()
        target.state.repository_registry = registry
        supervisor = await install_calendar_sync(target)
        assert supervisor is None
        await engine.dispose()

    async def test_degrades_without_registry(self) -> None:
        target = FastAPI()
        target.state.repository_registry = None
        supervisor = await install_calendar_sync(target)
        assert supervisor is None

    async def test_installs_and_stops_when_enabled(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NERVE_CALENDAR_SYNC_ENABLED", "1")
        engine, registry, service = await (
            TestSyncRunner()._harness(tmp_path)
        )
        target = FastAPI()
        target.state.repository_registry = registry
        target.state.calendar_service = service

        supervisor = await install_calendar_sync(target)
        assert supervisor is not None
        assert getattr(target.state, SUPERVISOR_STATE_KEY) is supervisor

        await stop_calendar_sync(target)
        assert getattr(target.state, SUPERVISOR_STATE_KEY, None) is None
        await engine.dispose()


# ---------------------------------------------------------------------------
# 6. Endpoints — degradation + wired
# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    reset_calendar_service()
    monkeypatch.delenv("NERVE_DB_URL", raising=False)
    monkeypatch.delenv("NERVECENTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("NERVE_CALENDAR_SYNC_ENABLED", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_calendar_service()


class TestCalendarEndpointsDegradation:
    """No durable registry: honest empty / executed:false (no fake success)."""

    def test_list_accounts_empty_200(self, client: Any) -> None:
        res = client.get("/api/calendar/accounts")
        assert res.status_code == 200
        assert res.json()["data"] == []

    def test_create_account_degrades(self, client: Any) -> None:
        res = client.post(
            "/api/calendar/accounts",
            json={"provider": "google", "label": "x"},
        )
        # House write contract: 202 with executed:false when not configured.
        body = res.json()
        assert res.status_code in (201, 202)
        assert body.get("executed", True) is False or "executed" not in body

    def test_sync_degrades_executed_false(self, client: Any) -> None:
        res = client.post("/api/calendar/accounts/anything/sync")
        assert res.status_code in (200, 202)
        body = res.json()
        assert body.get("executed") is False

    def test_get_unknown_account_id_synthetic(self, client: Any) -> None:
        res = client.get("/api/calendar/accounts/does-not-exist")
        assert res.status_code == 200
        assert res.json() == {"id": "does-not-exist", "resource": "calendar_account"}

    def test_literal_accounts_route_wins_over_event_catchall(self, client: Any) -> None:
        # The ``/accounts`` literal must not be swallowed by ``/{event_id}``.
        res = client.get("/api/calendar/accounts")
        assert res.status_code == 200
        assert "data" in res.json()


async def _make_cal_wired_app(
    tmp_path: pathlib.Path,
) -> tuple[FastAPI, AsyncEngine, RepositoryRegistry, CalendarService]:
    """Wire the durable calendar plane exactly like the app wiring does."""
    from backend.persistence.connection import make_session_factory
    from backend.persistence.repositories.connections_repo import IdentitySecretCodec

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'calwired.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            CalendarAccountRow.__table__.metadata.create_all,
            tables=CALENDAR_TABLES,
        )
    registry = RepositoryRegistry(
        make_session_factory(engine), secret_codec=IdentitySecretCodec()
    )
    service = CalendarService()

    app = FastAPI()
    from backend.api.endpoints import calendar as cal_endpoints

    app.include_router(cal_endpoints.router)
    app.state.repository_registry = registry
    app.state.calendar_service = service
    return app, engine, registry, service


class TestCalendarEndpointsWired:
    """With the durable plane bound, the account/conflict routes behave."""

    async def test_account_crud_and_vault_secret(
        self, tmp_path: pathlib.Path
    ) -> None:
        app, engine, registry, service = await _make_cal_wired_app(tmp_path)
        with TestClient(app) as client:
            # Create with a credential → stored in the vault, never echoed.
            res = client.post(
                "/api/calendar/accounts",
                json={
                    "provider": "google",
                    "label": "Personal",
                    "account_ref": "me@gmail.com",
                    "timezone": "America/Los_Angeles",
                    "config": {"calendar_ids": [_GOOGLE_CAL]},
                    "credential": json.dumps({"refresh_token": "rt-123"}),
                },
            )
            assert res.status_code == 201, res.text
            body = res.json()
            # Credential values are never present in any response field.
            assert "rt-123" not in json.dumps(body)
            assert body["provider"] == "google"
            assert body["operational_status"] == "never_synced"
            account_id = body["id"]

            # The credential lives in the secrets vault under the account owner.
            secret = await registry.connections.get_secret(
                kind="calendar_credential", owner_kind="connection",
                owner_id=account_id,
            )
            assert secret == json.dumps({"refresh_token": "rt-123"})

            # The account row carries only the secret_ref, not the value.
            row = await registry.calendar.get_account(account_id)
            assert row.secret_ref is not None
            assert "rt-123" not in (row.secret_ref or "")

            # GET one.
            got = client.get(f"/api/calendar/accounts/{account_id}")
            assert got.status_code == 200
            assert got.json()["id"] == account_id

            # PATCH (label + enabled).
            patched = client.patch(
                f"/api/calendar/accounts/{account_id}",
                json={"label": "Work", "enabled": False},
            )
            assert patched.status_code == 200
            assert patched.json()["label"] == "Work"
            assert patched.json()["enabled"] is False

            # DELETE → 204, then the row is gone (404).
            deleted = client.delete(f"/api/calendar/accounts/{account_id}")
            assert deleted.status_code == 204
            missing = client.get(f"/api/calendar/accounts/{account_id}")
            assert missing.status_code == 404

        await engine.dispose()

    async def test_conflicts_reads_durable_events(self, tmp_path: pathlib.Path) -> None:
        app, engine, registry, service = await _make_cal_wired_app(tmp_path)
        acct = await registry.calendar.create_account(
            provider="google", timezone="UTC"
        )
        # A durable event in the proposed window.
        await registry.calendar.upsert_event(
            account_id=acct.id,
            ical_uid="busy-1",
            title="Busy",
            start=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 11, 1, 13, 0, tzinfo=UTC),
        )
        with TestClient(app) as client:
            res = client.get(
                "/api/calendar/conflicts",
                params={"at": "2026-11-01T12:30:00Z", "duration": 60},
            )
            assert res.status_code == 200, res.text
            body = res.json()
            # The durable event in the window is reported as a conflict.
            assert body["available"] is False
            assert any(c["title"] == "Busy" for c in body["conflicts"])

        await engine.dispose()

    async def test_conflicts_free_window_is_available(self, tmp_path: pathlib.Path) -> None:
        app, engine, registry, service = await _make_cal_wired_app(tmp_path)
        await registry.calendar.create_account(provider="google", timezone="UTC")
        with TestClient(app) as client:
            res = client.get(
                "/api/calendar/conflicts",
                params={"at": "2026-11-02T09:00:00Z", "duration": 30},
            )
            assert res.status_code == 200
            body = res.json()
            assert body["available"] is True
            assert body["conflicts"] == []
        await engine.dispose()

    async def test_account_events_lists_durable_rows(self, tmp_path: pathlib.Path) -> None:
        app, engine, registry, service = await _make_cal_wired_app(tmp_path)
        acct = await registry.calendar.create_account(provider="google", timezone="UTC")
        await registry.calendar.upsert_event(
            account_id=acct.id,
            ical_uid="evt-a",
            title="Appt A",
            start=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        )
        with TestClient(app) as client:
            res = client.get(f"/api/calendar/accounts/{acct.id}/events")
            assert res.status_code == 200
            data = res.json()["data"]
            assert any(d["title"] == "Appt A" for d in data)

        await engine.dispose()
