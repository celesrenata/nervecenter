"""Workstream L: the Today landing page (``/api/today``).

Contract (frozen by spec §13):

- ``GET /api/today`` → ``{ needs_you, coming_up, handled, low_energy,
  simplified, generated_at }`` — no streak or "missed" fields by design.
- ``PATCH /api/today/mode`` persists the Low Energy Mode preference to the
  durable ``preferences`` table (key ``today.low_energy_mode``); ``GET``
  reflects it (needs_you truncated to the single top item, simplified True).

House degradation (Area 4): with no durable registry reads answer **200 with
empty sections** and the mode write answers ``executed: false`` — the page is
never a fake success and never a 5xx.

Acceptance covered here:
- row 7 (calendar on Today): a durable ``calendar_events`` row inside the
  48h window appears in ``coming_up``; outside it does not.
- row 15 (five frozen connection states): exercised by the Connections UI
  suite; today.py itself has no state axis, so nothing is asserted here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.app import create_app
from backend.persistence.models.personal import (
    AttentionItemRow,
    CalendarAccountRow,
    CalendarEventRow,
    PreferenceRow,
)
from backend.persistence.repositories import RepositoryRegistry

TODAY_TABLES = [
    AttentionItemRow.__table__,
    PreferenceRow.__table__,
    CalendarAccountRow.__table__,
    CalendarEventRow.__table__,
]

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)  # reference clock (tests)


# ---------------------------------------------------------------------------
# Fixture: today.router wired over a file-backed SQLite engine
# ---------------------------------------------------------------------------


async def _make_wired_app(tmp_path: Path) -> tuple[FastAPI, RepositoryRegistry]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'today.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(AttentionItemRow.__table__.metadata.create_all, tables=TODAY_TABLES)

    from backend.persistence.connection import make_session_factory

    registry = RepositoryRegistry(make_session_factory(engine))

    app = FastAPI()
    from backend.api.endpoints import today

    app.include_router(today.router)
    app.state.repository_registry = registry
    app.state._test_engine = engine  # disposed by the fixture finalizer
    return app, registry


@pytest.fixture
async def wired(tmp_path: Path):
    app, registry = await _make_wired_app(tmp_path)
    yield app, registry
    engine = app.state._test_engine
    await engine.dispose()


async def _insert(engine, *rows) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for row in rows:
            session.add(row)
        await session.commit()


def _item(**kw) -> AttentionItemRow:
    base = dict(
        importance=5,
        urgency="normal",
        state="active",
        source_refs=[],
        escalation_policy={},
    )
    base.update(kw)
    return AttentionItemRow(**base)


# ---------------------------------------------------------------------------
# Degradation contract (no durable registry — the default create_app())
# ---------------------------------------------------------------------------


class TestTodayDegradation:
    """``create_app()`` without a DB: honest empty sections, no fake success."""

    def test_get_today_empty_200_without_registry(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        monkeypatch.delenv("NERVECENTER_DATABASE_URL", raising=False)
        with TestClient(create_app()) as client:
            response = client.get("/api/today")
        assert response.status_code == 200
        body = response.json()
        assert body["needs_you"] == []
        assert body["coming_up"] == []
        assert body["handled"] == []
        assert body["low_energy"] is False
        assert body["simplified"] is False
        assert body["generated_at"]

    def test_patch_mode_degrades_honestly(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        monkeypatch.delenv("NERVECENTER_DATABASE_URL", raising=False)
        with TestClient(create_app()) as client:
            response = client.patch("/api/today/mode", json={"low_energy": True})
        # The handler has no status_code override → 200; the write degrades
        # honestly to executed: false / low_energy: false.
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["executed"] is False
        assert body["low_energy"] is False


# ---------------------------------------------------------------------------
# Wired contract: real durable rows over SQLite
# ---------------------------------------------------------------------------


class TestTodayWired:
    async def test_needs_you_ordering_importance_then_deadline(self, wired):
        app, _ = wired
        engine = app.state._test_engine
        await _insert(
            engine,
            _item(id=str(uuid4()), title="High, no deadline", importance=9),
            _item(id=str(uuid4()), title="High, due in 2h", importance=9,
                  deadline_at=NOW + timedelta(hours=2)),
            _item(id=str(uuid4()), title="High, due in 1h", importance=9,
                  deadline_at=NOW + timedelta(hours=1)),
            _item(id=str(uuid4()), title="Low, overdue", importance=3,
                  deadline_at=NOW - timedelta(hours=1)),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()

        titles = [it["title"] for it in body["needs_you"]]
        # importance desc; within the same importance deadline asc; no
        # deadline sorts last; lower importance last.
        assert titles == [
            "High, due in 1h",
            "High, due in 2h",
            "High, no deadline",
            "Low, overdue",
        ]

    async def test_needs_you_item_shape_matches_today_contract(self, wired):
        """C9: the web TodayView consumes exactly these fields."""
        app, _ = wired
        engine = app.state._test_engine
        await _insert(
            engine,
            _item(
                id=str(uuid4()),
                title="Call the bank",
                body="The card was declined twice.",
                importance=8,
                urgency="high",
                deadline_at=NOW + timedelta(hours=3),
                source_refs=["communication:c-123"],
                recommended_action="Call before 3pm",
            ),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()

        [item] = body["needs_you"]
        assert set(item) == {
            "id",
            "kind",
            "title",
            "body",
            "importance",
            "urgency",
            "deadline_at",
            "source_refs",
            "recommended_action",
        }
        assert item["kind"] == "attention"
        assert item["importance"] == 8
        assert item["urgency"] == "high"
        assert item["source_refs"] == ["communication:c-123"]
        assert item["recommended_action"] == "Call before 3pm"
        assert item["deadline_at"]

    async def test_coming_up_calendar_window(self, wired):
        """Row 7: durable calendar appointments land in Coming Up (48h)."""
        app, _ = wired
        engine = app.state._test_engine
        account = CalendarAccountRow(
            id=str(uuid4()), provider="google", label="Personal",
            account_ref="sam@example.com", timezone="UTC",
        )
        await _insert(engine, account)
        await _insert(
            engine,
            CalendarEventRow(
                id=str(uuid4()), account_id=account.id, ical_uid="uid-in",
                start=NOW + timedelta(hours=10), end=NOW + timedelta(hours=11),
                title="Lunch with Sam",
            ),
            CalendarEventRow(
                id=str(uuid4()), account_id=account.id, ical_uid="uid-out",
                start=NOW + timedelta(hours=72), end=NOW + timedelta(hours=73),
                title="Next week review",
            ),
            CalendarEventRow(
                id=str(uuid4()), account_id=account.id, ical_uid="uid-past",
                start=NOW - timedelta(hours=1), end=NOW,
                title="Already happened",
            ),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()

        assert [c["title"] for c in body["coming_up"]] == ["Lunch with Sam"]
        assert all(c["kind"] == "calendar" for c in body["coming_up"])
        assert body["coming_up"][0]["deadline_at"]

    async def test_handled_cap_and_24h_window(self, wired):
        app, _ = wired
        engine = app.state._test_engine
        recent = [
            _item(
                id=str(uuid4()),
                title=f"Resolved #{i}",
                state="resolved",
                resolved_at=NOW - timedelta(hours=i),  # i = 1..12, all < 24h
            )
            for i in range(1, 13)
        ]
        stale = _item(
            id=str(uuid4()),
            title="Resolved long ago",
            state="resolved",
            resolved_at=NOW - timedelta(hours=30),
        )
        await _insert(engine, *recent, stale)
        with TestClient(app) as client:
            body = client.get("/api/today").json()

        assert len(body["handled"]) == 10  # HANDLED_CAP
        titles = {h["title"] for h in body["handled"]}
        assert "Resolved long ago" not in titles
        assert "Resolved #1" in titles
        assert all(h["state"] == "resolved" for h in body["handled"])

    async def test_low_energy_truncates_to_single_item(self, wired):
        app, _ = wired
        engine = app.state._test_engine
        await _insert(
            engine,
            _item(id=str(uuid4()), title="A: due soon", importance=7,
                  deadline_at=NOW + timedelta(hours=1)),
            _item(id=str(uuid4()), title="B: no deadline", importance=7),
            _item(id=str(uuid4()), title="C: lower", importance=4,
                  deadline_at=NOW + timedelta(hours=2)),
        )
        # Full layout: all three active items surface.
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        assert len(body["needs_you"]) == 3
        assert body["low_energy"] is False
        assert body["simplified"] is False

        # Enable Low Energy Mode (durable preference).
        await _insert(
            engine,
            PreferenceRow(key="today.low_energy_mode", value={"low_energy": True}),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        assert body["low_energy"] is True
        assert body["simplified"] is True
        assert len(body["needs_you"]) == 1
        # Top item: importance desc, then deadline asc ("A: due soon").
        assert body["needs_you"][0]["title"] == "A: due soon"

        # Toggle off via the PATCH route.
        with TestClient(app) as client:
            response = client.patch("/api/today/mode", json={"low_energy": False})
        assert response.status_code == 200
        assert response.json() == {"low_energy": False}
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        assert len(body["needs_you"]) == 3
        assert body["simplified"] is False
