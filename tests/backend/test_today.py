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
from backend.obligations.service import register_obligations_today_provider
from backend.persistence.models.obligations import ObligationRow
from backend.persistence.models.personal import (
    AttentionItemRow,
    CalendarAccountRow,
    CalendarEventRow,
    CommitmentRow,
    ConnectionRow,
    PreferenceRow,
)
from backend.persistence.repositories import RepositoryRegistry

# The obligations Today provider is normally registered by wiring.py at app
# startup; in these unit tests we only mount ``today.router``, so register the
# (single, module-level) instance once at import — matching how the built-in
# providers register at import time. The ``is``-dedup keeps this stable across
# fixtures, so no per-test instance accumulation.
register_obligations_today_provider()

TODAY_TABLES = [
    AttentionItemRow.__table__,
    PreferenceRow.__table__,
    CalendarAccountRow.__table__,
    CalendarEventRow.__table__,
    CommitmentRow.__table__,
    ConnectionRow.__table__,
    ObligationRow.__table__,
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
    # Pin the endpoint clock so window-based assertions stay deterministic
    # regardless of the wall clock at run time (see get_today test seam).
    app.state._test_now = NOW
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


def _obligation_row(**kw) -> ObligationRow:
    base = dict(
        obligation_type="waiting_on",
        origin="inferred",
        source_account="gmail",
        dedup_key=f"waiting_on|test:{uuid4()}",
        title="Wait for reply",
        status="waiting",
        trigger={},
        uncertainty={},
        evidence=[],
    )
    base.update(kw)
    return ObligationRow(**base)


def _connection_row(**kw) -> ConnectionRow:
    base = dict(
        kind="gmail",
        label="Gmail",
        auth_status="authenticated",
        operational_status="healthy",
        last_success_at=NOW - timedelta(hours=1),
        last_attempt_at=NOW - timedelta(hours=1),
        sync_cursor={},
        import_progress={},
        enabled=True,
        config={},
    )
    base.update(kw)
    return ConnectionRow(**base)


# ---------------------------------------------------------------------------
# P2-F: Waiting On + Source Health sections
# ---------------------------------------------------------------------------


class TestTodayP2FSections:
    """P2-F §4/§5: ``waiting_on`` and ``source_health`` in the Today response."""

    async def test_response_shape_includes_waiting_on_and_source_health(
        self, wired
    ):
        app, _ = wired
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        assert body["waiting_on"] == []
        assert body["source_health"] == []
        # The two new sections never 500 on an empty DB.
        assert body["low_energy"] is False

    async def test_waiting_on_from_durable_obligation_rows(self, wired):
        """A durable ``obligation`` row of type ``waiting_on`` surfaces in
        the Waiting On section with the Today item shape."""
        app, _ = wired
        engine = app.state._test_engine
        await _insert(
            engine,
            _obligation_row(
                title="Waiting on Dana for the contract",
                due_at=NOW + timedelta(hours=3),
                next_action="Nudge Dana",
            ),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        assert len(body["waiting_on"]) == 1
        item = body["waiting_on"][0]
        assert item["kind"] == "obligation"
        assert item["title"] == "Waiting on Dana for the contract"
        assert item["deadline_at"]
        assert item["next_action"] == "Nudge Dana"
        # Not duplicated into needs_you / coming_up.
        assert all(i.get("id") != item["id"] for i in body["needs_you"])
        assert all(c.get("id") != item["id"] for c in body["coming_up"])

    async def test_waiting_on_collapses_to_near_due_under_low_energy(
        self, wired
    ):
        """Low Energy keeps only the near-due waiting items (48h window)."""
        app, _ = wired
        engine = app.state._test_engine
        near = _obligation_row(title="Near", due_at=NOW + timedelta(hours=6))
        far = _obligation_row(title="Far", due_at=NOW + timedelta(days=30))
        await _insert(
            engine,
            near,
            far,
            PreferenceRow(key="today.low_energy_mode", value={"low_energy": True}),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        assert body["low_energy"] is True
        assert [w["title"] for w in body["waiting_on"]] == ["Near"]

    async def test_source_health_fresh_stale_disconnected(self, wired):
        """Stale connector data is visibly distinct from an empty inbox."""
        app, _ = wired
        engine = app.state._test_engine
        await _insert(
            engine,
            # Healthy: last sync 1h ago.
            _connection_row(kind="gmail", label="Gmail"),
            # Stale: last success 48h ago.
            _connection_row(
                kind="imap",
                label="Old IMAP",
                last_success_at=NOW - timedelta(hours=48),
                last_attempt_at=NOW - timedelta(hours=48),
            ),
            # Disconnected: auth expired.
            _connection_row(
                kind="imessage",
                label="iMessage",
                auth_status="auth_expired",
                operational_status="error",
                last_success_at=NOW - timedelta(days=10),
                last_error_message="token expired",
            ),
            # Disabled: must not appear.
            _connection_row(kind="calendar", label="Disabled", enabled=False),
        )
        with TestClient(app) as client:
            body = client.get("/api/today").json()
        health = {h["label"]: h for h in body["source_health"]}
        assert set(health) == {"Gmail", "Old IMAP", "iMessage"}
        assert health["Gmail"]["status"] == "fresh"
        assert health["Gmail"]["kind"] == "source:gmail"
        assert health["Old IMAP"]["status"] == "stale"
        assert health["iMessage"]["status"] == "disconnected"
        assert health["iMessage"]["detail"] == "token expired"
        assert "Disabled" not in health
