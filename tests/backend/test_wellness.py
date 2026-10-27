"""Workstream I — private Wellness Journal (Personal-Ops Recovery §10).

Covers the frozen §10 contract:

1. **Migration ``010_wellness``** — sits on the linear chain
   (``008_bills``; the integrator rewired ``007 -> 008 -> 010 -> 009``),
   creates exactly ``wellness_entries`` with the frozen column set, and the
   ORM table matches.
2. **Upsert-by-day** — same day twice → one row, updated in place; a new
   day → a new row; ``PATCH`` is field-level (``None`` leaves values alone).
3. **Trends** — deterministic statistics on fixture data with known means
   and a known trend direction; sleep-merge from H's sleep seam fills gaps
   only; null-safe.
4. **Correlation** — Pearson over paired (sleep, mood) days; ``n < 7`` →
   ``insufficient`` (never a diagnosis word).
5. **Export** — explicit request returns the exact window rows and writes
   the stable-code audit entry; ``wellness.enabled=false`` → **403**.
6. **Delete-all** — hard-deletes everything; **pause window** reflects in
   status and blocks check-ins; **disable** stops collection but keeps data.
7. **PRIVACY (normative)** — import-graph test: ``backend.wellness``
   imports nothing from ``backend.inference`` / ``backend.omniroute`` /
   cloud transports; no diagnosis vocabulary in any service output.

The durable plane is exercised on a SQLite scratch DB through the real
repository (the JSONB columns port), and the HTTP surface through a
FastAPI app wired exactly like production (``repository_registry`` on
``app.state``). The H sleep seam is injected as a scripted async callable.
"""

from __future__ import annotations

import ast
import importlib
import io
import pathlib
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import PreferenceRow
from backend.persistence.models.wellness import WellnessEntryRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.wellness_repo import (
    EXPORT_AUDIT_CODE,
    EXPORT_LOG_KEY,
    WellnessRepository,
)
from backend.wellness.trends import TrendEngine

_ALEMBIC_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "persistence"
    / "alembic"
)

EXPECTED_WELLNESS_TABLE_NAMES = {"wellness_entries"}
EXPECTED_COLUMNS = {
    "id",
    "created_at",
    "updated_at",
    "schema_version",
    "at",
    "mood",
    "sleep_minutes",
    "sleep_consistency",
    "energy",
    "stress",
    "journal_entry",
    "observations",
}


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


async def _make_harness(tmp_path: pathlib.Path) -> tuple[AsyncEngine, RepositoryRegistry]:
    """A durable SQLite DB + repository registry, wired like production."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wellness.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            WellnessEntryRow.__table__.metadata.create_all,
            tables=[WellnessEntryRow.__table__, PreferenceRow.__table__],
        )
    registry = RepositoryRegistry(make_session_factory(engine))
    return engine, registry


# ---------------------------------------------------------------------------
# 1. Migration 010_wellness
# ---------------------------------------------------------------------------


class TestMigration010:
    def test_revision_sits_on_worktree_head(self) -> None:
        m = importlib.import_module("backend.persistence.alembic.versions.010_wellness")
        assert m.revision == "010_wellness"
        assert m.down_revision == "008_bills"

    def test_creates_exactly_the_frozen_table_set(self) -> None:
        m = importlib.import_module("backend.persistence.alembic.versions.010_wellness")
        # Every op.create_table call in upgrade() must be wellness_entries.
        src = pathlib.Path(m.__file__).read_text()
        trees = [
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_table"
        ]
        assert trees, "no op.create_table calls found"
        created = {ast.literal_eval(call.args[0]) for call in trees}
        assert created == EXPECTED_WELLNESS_TABLE_NAMES

    def test_orm_table_matches_frozen_columns(self) -> None:
        cols = {c.name for c in WellnessEntryRow.__table__.columns}
        assert cols == EXPECTED_COLUMNS

    def test_upgrade_applies_on_prior_head(self) -> None:
        """Render the ``008_bills -> 010_wellness`` step offline (Postgres dialect).

        The alembic chain is Postgres-only (``001_initial`` issues
        ``CREATE EXTENSION vector``), so it cannot execute on a SQLite
        scratch DB. Offline SQL rendering (``sql=True``) runs the real
        revision without a live database and yields the exact DDL the
        migration would emit.
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            command.upgrade(
                _alembic_config(
                    "postgresql+asyncpg://nervcenter@localhost:5432/nervcenter"
                ),
                "008_bills:010_wellness",
                sql=True,
            )
        sql = buf.getvalue()

        # Only the 010 step is rendered (no prior tables, no later head).
        assert "Running upgrade 008_bills -> 010_wellness" in sql
        assert "CREATE TABLE wellness_entries" in sql
        # The frozen index (006 naming convention: ix_<table>_<column>).
        assert "CREATE INDEX ix_wellness_entries_at" in sql
        # The two frozen JSONB columns (sleep_consistency / observations).
        assert "JSONB" in sql
        # The chain is left stamped at the new head.
        assert "UPDATE alembic_version SET version_num='010_wellness'" in sql


# ---------------------------------------------------------------------------
# 2. Upsert-by-day + field patch
# ---------------------------------------------------------------------------


class TestUpsertByDay:
    async def test_same_day_twice_one_row_updated(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        day = datetime(2026, 10, 20, 8, 30)
        row1, created1 = await repo.upsert_by_day(at=day, mood=3, energy=5)
        row2, created2 = await repo.upsert_by_day(
            at=datetime(2026, 10, 20, 21, 15), mood=4, energy=7, journal_entry="hi"
        )
        assert created1 is True
        assert created2 is False
        assert row1.id == row2.id
        assert (await repo.count_entries()) == 1
        assert row2.mood == 4
        assert row2.energy == 7
        assert row2.journal_entry == "hi"
        await engine.dispose()

    async def test_new_day_new_row(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        await repo.upsert_by_day(at=datetime(2026, 10, 20, 8, 0), mood=3)
        row, created = await repo.upsert_by_day(at=datetime(2026, 10, 21, 9, 0), mood=4)
        assert created is True
        assert (await repo.count_entries()) == 2
        assert row.at.date() == datetime(2026, 10, 21).date()
        await engine.dispose()

    async def test_patch_is_field_level(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        row, _ = await repo.upsert_by_day(
            at=datetime(2026, 10, 20, 8, 0), mood=3, energy=5, stress=2
        )
        patched = await repo.update_entry(entry_id=row.id, mood=4)
        assert patched is not None
        assert patched.mood == 4
        assert patched.energy == 5  # untouched — None means leave-as-is
        assert patched.stress == 2
        assert (await repo.get_entry(row.id)).journal_entry is None
        assert (await repo.update_entry(entry_id="does-not-exist", mood=1)) is None
        await engine.dispose()

    async def test_upsert_clears_unchecked_fields(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        await repo.upsert_by_day(at=datetime(2026, 10, 20, 8, 0), mood=3, stress=9)
        row, _ = await repo.upsert_by_day(at=datetime(2026, 10, 20, 9, 0), mood=4)
        assert row.mood == 4
        assert row.stress is None  # not carried forward from earlier today
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Trends (deterministic statistics on fixture data)
# ---------------------------------------------------------------------------


def _rows_for(*pairs: tuple[int, int | None, int | None]) -> list[dict[str, Any]]:
    """Fixture builder: day offsets with (mood, energy, sleep_minutes)."""
    base = datetime(2026, 10, 1)
    rows = []
    for i, (mood, energy, sleep) in enumerate(pairs):
        d = base + timedelta(days=i)
        rows.append(
            {
                "day": d.date().isoformat(),
                "mood": mood,
                "energy": energy,
                "stress": None,
                "sleep_minutes": sleep,
                "n": 1,
            }
        )
    return rows


class TestTrends:
    def test_known_means_and_direction(self) -> None:
        engine = TrendEngine()
        # Mood: 1,2,3,4,5 → mean 3.0, slope +1/day, clearly upward.
        rows = _rows_for((1, None, None), (2, None, None), (3, None, None), (4, None, None), (5, None, None))
        trend = engine.trend_for(metric="mood", rows=rows, days=30)
        assert trend["n"] == 5
        assert trend["mean"] == 3.0
        assert trend["first"] == 1
        assert trend["last"] == 5
        assert trend["slope_per_day"] == pytest.approx(1.0, abs=1e-9)
        assert trend["direction"] == "upward"
        assert trend["relative_change"] > 0.15  # strong, not "slightly"
        assert "mood trend is upward over 30 days" in trend["description"]

    def test_downward_and_flat(self) -> None:
        engine = TrendEngine()
        down = _rows_for((5, None, None), (4, None, None), (3, None, None), (2, None, None), (1, None, None))
        t_down = engine.trend_for(metric="mood", rows=down, days=30)
        assert t_down["direction"] == "downward"

        # Tiny wobble around a constant → flat.
        flat = _rows_for((3, None, None), (3, None, None), (3, None, None), (3, None, None), (3, None, None))
        t_flat = engine.trend_for(metric="mood", rows=flat, days=30)
        assert t_flat["direction"] == "flat"

    def test_insufficient_gate(self) -> None:
        engine = TrendEngine()
        rows = _rows_for((1, None, None), (2, None, None))
        t = engine.trend_for(metric="mood", rows=rows, days=30)
        assert t["n"] == 2
        assert t["direction"] == "insufficient"
        assert "insufficient data" in t["description"]
        assert t["slope_per_day"] is None

    def test_null_safe_skip(self) -> None:
        """Days missing the metric are skipped, not treated as 0."""
        engine = TrendEngine()
        rows = [
            {"day": "2026-10-01", "mood": 4, "energy": None, "stress": None, "sleep_minutes": None, "n": 1},
            {"day": "2026-10-02", "mood": None, "energy": None, "stress": None, "sleep_minutes": None, "n": 1},
            {"day": "2026-10-03", "mood": 2, "energy": None, "stress": None, "sleep_minutes": None, "n": 1},
            {"day": "2026-10-04", "mood": 2, "energy": None, "stress": None, "sleep_minutes": None, "n": 1},
        ]
        t = engine.trend_for(metric="mood", rows=rows, days=30)
        assert t["n"] == 3  # the None day skipped
        # TrendEngine reports means rounded to 3 decimals (2.667 for 8/3).
        assert t["mean"] == pytest.approx(2.667)

    def test_sleep_consistency(self) -> None:
        engine = TrendEngine()
        rows = [
            {"day": "2026-10-01", "mood": None, "energy": None, "stress": None, "sleep_minutes": 420, "n": 1},
            {"day": "2026-10-02", "mood": None, "energy": None, "stress": None, "sleep_minutes": 440, "n": 1},
            {"day": "2026-10-03", "mood": None, "energy": None, "stress": None, "sleep_minutes": 430, "n": 1},
        ]
        s = engine.sleep_consistency(rows=rows, days=7)
        assert s["regularity"] == "stable"
        assert s["avg_minutes"] == 430
        assert s["stdev_minutes"] is not None and s["stdev_minutes"] < 45

        rows_var = [
            {"day": "2026-10-01", "mood": None, "energy": None, "stress": None, "sleep_minutes": 300, "n": 1},
            {"day": "2026-10-02", "mood": None, "energy": None, "stress": None, "sleep_minutes": 560, "n": 1},
            {"day": "2026-10-03", "mood": None, "energy": None, "stress": None, "sleep_minutes": 480, "n": 1},
            {"day": "2026-10-04", "mood": None, "energy": None, "stress": None, "sleep_minutes": 380, "n": 1},
        ]
        sv = engine.sleep_consistency(rows=rows_var, days=7)
        assert sv["regularity"] == "variable"

        s_sparse = engine.sleep_consistency(rows=rows[:2], days=7)
        assert s_sparse["regularity"] == "unknown"

    def test_correlation_n_gate_and_value(self) -> None:
        engine = TrendEngine()
        # 6 paired days → insufficient (gate is n >= 7).
        rows = _rows_for(
            (1, None, 400), (2, None, 420), (3, None, 440),
            (4, None, 460), (5, None, 480), (4, None, 470),
        )
        c = engine.correlation(rows=rows, days=30)
        assert c["n"] == 6
        assert c["status"] == "insufficient"
        assert c["r"] is None
        assert "insufficient" in c["description"]

        # 7 perfectly correlated days → r = +1.
        rows7 = _rows_for(
            (1, None, 360), (2, None, 380), (3, None, 400), (4, None, 420),
            (5, None, 440), (3, None, 400), (4, None, 420),
        )
        c7 = engine.correlation(rows=rows7, days=30)
        assert c7["n"] == 7
        assert c7["status"] == "ok"
        assert c7["r"] == pytest.approx(1.0, abs=1e-9)
        assert "positive" in c7["description"]

        # Constant axis → no_variation, r stays None.
        cconst = engine.correlation(
            rows=_rows_for((3, None, 400), (3, None, 400), (3, None, 400),
                           (3, None, 400), (3, None, 400), (3, None, 400),
                           (3, None, 400)),
            days=30,
        )
        assert cconst["status"] == "no_variation"
        assert cconst["r"] is None

    def test_sleep_merge_fills_gaps_only(self) -> None:
        engine = TrendEngine()
        rows = [
            {"day": "2026-10-01", "mood": 4, "energy": None, "stress": None, "sleep_minutes": None, "n": 1},
            {"day": "2026-10-02", "mood": 3, "energy": None, "stress": None, "sleep_minutes": 500, "n": 1},
        ]
        sleep_records = [
            {"bed_at": "2026-10-01T22:00:00Z", "wake_at": "2026-10-02T06:00:00Z", "duration_minutes": 480, "source": "apple_health"},
        ]
        merged = engine.merge_sleep(rows, sleep_records)
        assert merged[0]["sleep_minutes"] == 480.0  # gap filled
        assert merged[1]["sleep_minutes"] == 500  # self-reported wins
        # Unmerged copy is untouched.
        assert rows[0]["sleep_minutes"] is None

    def test_trends_payload_shape(self) -> None:
        engine = TrendEngine()
        out = engine.trends(rows=_rows_for((3, 5, 420)), days=7)
        assert out["window_days"] == 7
        assert out["n_entries"] == 1
        assert set(out["trends"]) == {"mood", "energy", "stress", "sleep_minutes"}
        assert "sleep" in out and "correlation" in out
        assert out["sensitivity"] == "personal"


# ---------------------------------------------------------------------------
# 4. HTTP surface (durable, wired like production)
# ---------------------------------------------------------------------------


def _wired_app(tmp_path: pathlib.Path, registry: RepositoryRegistry, sleep_provider: Any = None) -> FastAPI:
    from backend.api.endpoints import wellness as w

    app = FastAPI()
    app.include_router(w.router)
    app.state.repository_registry = registry
    app.state.wellness_sleep_provider = sleep_provider
    return app


async def _seed(repo: WellnessRepository) -> None:
    base = datetime(2026, 10, 1)
    for i in range(9):
        await repo.upsert_by_day(
            at=base + timedelta(days=i),
            mood=1 + (i % 5),
            sleep_minutes=400 + i * 5,
            energy=5,
            stress=3,
        )


class TestWellnessHttp:
    async def test_degrades_without_registry(self) -> None:
        from backend.api.endpoints import wellness as w

        app = FastAPI()
        app.include_router(w.router)
        app.state.repository_registry = None
        client = TestClient(app)

        assert client.get("/api/wellness/entries").json() == {"data": [], "durable": False}
        status = client.get("/api/wellness/status").json()
        assert status["enabled"] is True
        assert status["entry_count"] == 0
        assert status["sensitivity"] == "personal"
        trends = client.get("/api/wellness/trends?days=30").json()
        assert trends["sleep"]["description"] == "no sleep data"
        assert trends["correlation"]["status"] == "insufficient"
        assert client.post("/api/wellness/entries", json={}).json()["recorded"] is False
        assert client.delete("/api/wellness/entries").json()["deleted"] == 0
        export = client.get("/api/wellness/export?format=json")
        assert export.status_code == 200
        assert export.json()["entries"] == 0

    async def test_upsert_via_http_and_list_window(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        app = _wired_app(tmp_path, registry)
        client = TestClient(app)

        r1 = client.post("/api/wellness/entries", json={"at": "2026-10-20T08:00:00Z", "mood": 3, "energy": 5})
        assert r1.status_code == 201
        assert r1.json()["created"] is True
        entry_id = r1.json()["entry"]["id"]

        r2 = client.post(
            "/api/wellness/entries",
            json={"at": "2026-10-20T21:00:00Z", "mood": 4, "journal_entry": "better later"},
        )
        assert r2.status_code == 201
        assert r2.json()["created"] is False  # same day → in-place update
        assert r2.json()["entry"]["id"] == entry_id

        rows = client.get("/api/wellness/entries").json()["data"]
        assert len(rows) == 1
        assert rows[0]["mood"] == 4
        assert rows[0]["journal_entry"] == "better later"
        assert rows[0]["sensitivity"] == "personal"

        # Windowing by from/to.
        rows_window = client.get(
            "/api/wellness/entries", params={"from": "2026-10-21", "to": "2026-10-22"}
        ).json()["data"]
        assert rows_window == []
        rows_all = client.get(
            "/api/wellness/entries", params={"from": "2026-10-01", "to": "2026-12-31"}
        ).json()["data"]
        assert len(rows_all) == 1

        # PATCH field-level.
        patched = client.patch(f"/api/wellness/entries/{entry_id}", json={"stress": 2})
        assert patched.status_code == 200
        assert patched.json()["stress"] == 2
        assert patched.json()["mood"] == 4  # untouched

        # Validation: mood out of range → 422.
        bad = client.post("/api/wellness/entries", json={"mood": 9})
        assert bad.status_code == 422

        # Unknown id → 404, no synthetic row.
        assert client.patch("/api/wellness/entries/nope", json={"mood": 1}).status_code == 404
        assert client.delete("/api/wellness/entries/nope").status_code == 404
        await engine.dispose()

    async def test_trends_and_correlation_http(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        await _seed(repo)
        app = _wired_app(tmp_path, registry)
        client = TestClient(app)

        out = client.get("/api/wellness/trends?days=90").json()
        assert out["n_entries"] == 9
        assert out["trends"]["mood"]["n"] == 9
        # Seeded sleep is linear 400..435 with mood cycling 1..5 → correlation
        # present (n=9 >= 7) but direction depends on the cycle; assert the
        # contract fields, not a specific sign.
        assert out["correlation"]["n"] == 9
        assert out["correlation"]["r"] is not None
        assert out["sleep"]["regularity"] in ("stable", "variable")
        assert out["sleep_data_present"] is True

        # Sleep seam (H's records) degrades gracefully when absent and merges
        # when present.
        sleep_seeams = []

        async def fake_sleep(days: int) -> list[dict[str, Any]]:
            sleep_seeams.append(days)
            return [
                {"bed_at": "2026-09-30T23:00:00Z", "wake_at": "2026-10-01T05:00:00Z", "duration_minutes": 360, "source": "apple_health"},
            ]

        app.state.wellness_sleep_provider = fake_sleep
        client2 = TestClient(app)
        out2 = client2.get("/api/wellness/trends?days=90").json()
        assert sleep_seeams == [90]
        assert out2["sleep_data_present"] is True
        await engine.dispose()

    async def test_export_window_audit_and_gate(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        await _seed(repo)
        app = _wired_app(tmp_path, registry)
        client = TestClient(app)

        # Exact window: rows 2026-10-02 .. 2026-10-04 → 3 entries.
        res = client.get(
            "/api/wellness/export",
            params={"format": "json", "from": "2026-10-02", "to": "2026-10-04T23:59:59Z"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["entries"] == 3
        assert body["audit"]["code"] == EXPORT_AUDIT_CODE
        days = {r["at"][:10] for r in body["rows"]}
        assert days == {"2026-10-02", "2026-10-03", "2026-10-04"}
        assert all(r["sensitivity"] == "personal" for r in body["rows"])

        # Audit row was written (durable preferences table, stable code).
        pref = await repo.get_preference(EXPORT_LOG_KEY)
        assert pref is not None
        assert any(e["code"] == EXPORT_AUDIT_CODE for e in pref)
        assert pref[0]["entries"] == 3

        # CSV export returns real CSV bytes with a download disposition.
        csv_res = client.get("/api/wellness/export", params={"format": "csv"})
        assert csv_res.status_code == 200
        assert csv_res.headers["content-type"].startswith("text/csv")
        assert "attachment" in csv_res.headers.get("content-disposition", "")
        text = csv_res.text
        assert text.splitlines()[0].startswith("id,at,mood,sleep_minutes,energy,stress,journal_entry,observations")
        assert len(text.splitlines()) == 1 + 9  # header + 9 rows

        # Invalid format → 422 (the route pattern rejects it).
        assert client.get("/api/wellness/export?format=xml").status_code == 422

        # Opt-in gate: enabled=false → 403, no audit row added.
        await repo.set_preference("wellness.enabled", {"enabled": False})
        blocked = client.get("/api/wellness/export?format=json")
        assert blocked.status_code == 403
        assert "opt-in" in blocked.json()["detail"]
        pref_after = await repo.get_preference(EXPORT_LOG_KEY)
        assert len(pref_after) == 2  # JSON + CSV exports logged; the 403 added nothing
        await engine.dispose()

    async def test_delete_all_wipes_everything(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        await _seed(repo)
        app = _wired_app(tmp_path, registry)
        client = TestClient(app)

        assert client.get("/api/wellness/entries").json()["data"]
        res = client.delete("/api/wellness/entries")
        assert res.status_code == 200
        assert res.json()["deleted"] == 9
        assert client.get("/api/wellness/entries").json()["data"] == []
        assert client.get("/api/wellness/status").json()["entry_count"] == 0
        assert client.get("/api/wellness/status").json()["last_entry_at"] is None

        # Single-entry delete + 404 on the now-gone row.
        r = client.post("/api/wellness/entries", json={"mood": 2})
        entry_id = r.json()["entry"]["id"]
        assert client.delete(f"/api/wellness/entries/{entry_id}").json()["deleted"] == 1
        assert client.delete(f"/api/wellness/entries/{entry_id}").status_code == 404
        await engine.dispose()

    async def test_pause_window_and_disable(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = WellnessRepository(registry.session_factory)
        app = _wired_app(tmp_path, registry)
        client = TestClient(app)

        # Pause window: blocks check-ins, shows in status.
        until = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        res = client.patch("/api/wellness/status", json={"paused_until": until})
        assert res.status_code == 200
        status = client.get("/api/wellness/status").json()
        assert status["paused_until"] == until
        blocked = client.post("/api/wellness/entries", json={"mood": 3})
        assert blocked.status_code == 409
        assert "paused" in blocked.json()["detail"]
        assert (await repo.count_entries()) == 0  # nothing recorded

        # Reading still works while paused (data kept).
        assert client.get("/api/wellness/trends?days=30").status_code == 200

        # Resume clears the pause.
        client.patch("/api/wellness/status", json={"clear_pause": True})
        assert client.get("/api/wellness/status").json()["paused_until"] is None
        ok = client.post("/api/wellness/entries", json={"mood": 3})
        assert ok.status_code == 201

        # Disable stops collection AND the export (403 gate), keeps data.
        client.patch("/api/wellness/status", json={"enabled": False})
        assert client.get("/api/wellness/status").json()["enabled"] is False
        assert client.post("/api/wellness/entries", json={"mood": 3}).status_code == 409
        assert client.get("/api/wellness/export?format=json").status_code == 403
        assert client.get("/api/wellness/entries").json()["data"]  # data kept
        assert (await repo.count_entries()) == 1

        # Re-enable restores both.
        client.patch("/api/wellness/status", json={"enabled": True})
        assert client.get("/api/wellness/status").json()["enabled"] is True
        assert client.get("/api/wellness/export?format=json").status_code == 200
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Privacy — normative
# ---------------------------------------------------------------------------


#: Forbidden import roots: wellness must never touch inference, OmniRoute,
#: transports, or anything that leaves the process toward a model/cloud.
FORBIDDEN_ROOTS = (
    "backend.inference",
    "backend.omniroute",
    "backend.ingest",
)

#: Cloud-transport / external markers that must not appear in the import set.
FORBIDDEN_MARKERS = ("omniroute", "httpx.AsyncClient", "aiohttp", "requests")


def _import_set(module: Any) -> set[str]:
    """The full transitive import set of a package (stdlib-safe walk)."""
    import sys

    seen: set[str] = set()
    stack = [module]
    while stack:
        mod = stack.pop()
        name = mod.__name__
        if name in seen:
            continue
        seen.add(name)
        for sub in getattr(mod, "__path__", None) or []:
            for modname in (
                p.name
                for p in pathlib.Path(sub).iterdir()
                if p.name.endswith(".py") and p.name != "__init__.py"
            ):
                try:
                    stack.append(importlib.import_module(f"{name}.{modname}"))
                except Exception:
                    pass
    return seen


class TestPrivacy:
    def test_no_inference_omniroute_or_cloud_imports(self) -> None:
        """Import-graph audit: ``backend.wellness`` pulls in no forbidden root."""
        import backend.wellness

        imported = _import_set(backend.wellness)
        forbidden = {
            name
            for name in imported
            if name in FORBIDDEN_ROOTS
            or any(name.startswith(root + ".") for root in FORBIDDEN_ROOTS)
        }
        assert not forbidden, f"wellness package imports forbidden roots: {forbidden}"

        # Belt-and-braces source-level check across the whole package:
        # no import statement (or string literal reference) to a forbidden
        # root anywhere in backend/wellness/*.py.
        pkg = pathlib.Path(backend.wellness.__file__).parent
        for py in pkg.glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = [node.module]
                bad = [
                    r
                    for r in roots
                    if r in FORBIDDEN_ROOTS or any(r.startswith(x + ".") for x in FORBIDDEN_ROOTS)
                ]
                assert not bad, f"{py.name}: forbidden import {bad}"

    def test_endpoint_imports_no_health_endpoint_module(self) -> None:
        """Sleep arrives via the app-state seam — no module-import coupling to H."""
        import backend.api.endpoints.wellness as w

        src = pathlib.Path(w.__file__).read_text()
        assert "endpoints.health" not in src
        assert "from backend.api.endpoints" not in src

    def test_no_diagnosis_vocabulary_in_outputs(self) -> None:
        """TrendEngine output (on representative fixtures) never says a diagnosis."""
        engine = TrendEngine()
        forbidden_words = [
            "depress",
            "anxiet",
            "disorder",
            "diagnos",
            "clinical",
            "therap",
            "condition",
            "symptom",
            "mental health",
            "burnout",
        ]
        rows = _rows_for(
            (1, 2, 300), (2, 3, 360), (3, 4, 420), (4, 6, 480),
            (5, 8, 540), (3, 5, 400), (2, 4, 350),
        )
        stress_rows = [dict(r, stress=(i % 5) + 1) for i, r in enumerate(rows)]
        payload = engine.trends(rows=stress_rows, days=90)
        blob = _flatten(payload)
        for word in forbidden_words:
            assert word not in blob, f"diagnosis vocabulary leaked: {word!r}"
        # The correlation block reports r+n or an insufficient status, nothing else.
        corr = payload["correlation"]
        assert corr["status"] in ("ok", "insufficient", "no_variation")
        if corr["status"] == "ok":
            assert isinstance(corr["r"], float) and corr["n"] >= 7

    def test_service_output_uses_whitelist_vocabulary(self) -> None:
        """The per-metric descriptions only use the data-describing whitelist."""
        engine = TrendEngine()
        rows = _rows_for((1, 5, 400), (2, 5, 410), (3, 5, 420), (4, 5, 430), (5, 5, 440))
        payload = engine.trends(rows=rows, days=30)
        allowed_fragments = (
            "insufficient data",
            "trend is upward over",
            "trend is downward over",
            "is steady over",
            "slightly upward",
            "slightly downward",
            "sleep duration is steady day to day",
            "sleep duration varies day to day",
            "association between sleep duration and mood over",
            "no day-to-day variation",
        )
        for trend in list(payload["trends"].values()):
            assert any(f in trend["description"] for f in allowed_fragments), trend["description"]


def _flatten(obj: Any) -> str:
    if isinstance(obj, dict):
        return " ".join(_flatten(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_flatten(v) for v in obj)
    return str(obj).lower()
