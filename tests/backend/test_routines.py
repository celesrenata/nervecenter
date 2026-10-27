"""Workstream H — the Routine Engine (Personal-Ops Recovery §9).

Covers the frozen §9 contract:

1. **Schedule evaluation** — per-routine wall-clock slots from
   ``schedule.times`` on the scheduled ``weekdays`` (ISO Monday=0; empty
   list = every day), in the routine's own IANA ``timezone``: due-now
   windows, passed-unmet windows, and a weekday-boundary case (a slot that
   does NOT occur on a non-scheduled weekday).
2. **Missed window flagged EXACTLY once** — two evaluations of the same
   passed-unmet window produce one gentle note, and ``missed_flags`` holds
   one key (one-time marker — no streak, no re-flag).
3. **Wake fallback alarm** — deterministic clock-based: computed from the
   schedule (next local slot in the routine's timezone), persisted to
   ``deterministic_fallback_alarm`` with the frozen shape
   ``{at, timezone, kind: "clock_fallback", trigger_level}``.
4. **Sleep regularity** — ``unknown`` with fewer than 3 records, and
   NOT ``unknown`` with enough (stable/variable from the duration stdev).
5. **Check-in idempotency** — same day twice → one row.
6. **Complete clears the window flag** — the matching ``missed_flags``
   key is removed when the routine is completed.
7. **Endpoint CRUD + degradation** — without a registry the GET routes
   answer 200 empty and the write routes answer 202
   ``executed: false``.
8. **Today provider** — ``coming_up`` / ``handled`` output, ``needs_you``
   empty, and idempotent registration (same pattern as
   ``tests/backend/test_today.py`` / the bills provider).
9. **Wellness sleep seam** — ``make_wellness_sleep_provider`` returns ISO
   ``bed_at``/``wake_at`` + ``source`` (``wake_at`` may be ``None``) and
   ``[]`` when degraded.

The durable plane is exercised on a SQLite scratch DB through the real
repository (the JSONB columns port), and the HTTP surface through a FastAPI
app wired exactly like production (``repository_registry`` on ``app.state``).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.connection import make_session_factory
from backend.persistence.models.routines import (
    RoutineCheckInRow,
    RoutineRow,
    SleepRecordRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.routines_repo import RoutinesRepository
from backend.routines.service import (
    DUE_WINDOW_MINUTES,
    RoutinesService,
    RoutinesTodayProvider,
    compute_wake_fallback_alarm,
    make_wellness_sleep_provider,
    register_routines_today_provider,
)

ROUTINE_TABLES = [
    RoutineRow.__table__,
    RoutineCheckInRow.__table__,
    SleepRecordRow.__table__,
]


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


async def _make_harness(tmp_path: pathlib.Path) -> tuple[AsyncEngine, RepositoryRegistry]:
    """A durable SQLite DB + repository registry, wired like production."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'routines.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(RoutineRow.__table__.metadata.create_all, tables=ROUTINE_TABLES)
    registry = RepositoryRegistry(make_session_factory(engine))
    return engine, registry


def _repo(registry: RepositoryRegistry) -> RoutinesRepository:
    return RoutinesRepository(registry.session_factory)


def _svc(registry: RepositoryRegistry) -> RoutinesService:
    return RoutinesService(_repo(registry))


async def _seed_routine(
    registry: RepositoryRegistry,
    *,
    kind: str = "wake",
    title: str = "Wake up",
    schedule: dict[str, Any] | None = None,
    enabled: bool = True,
) -> str:
    row = await _repo(registry).create_routine(
        kind=kind, title=title, schedule=schedule, enabled=enabled
    )
    return row.id


# ---------------------------------------------------------------------------
# 1. Schedule evaluation (times / weekdays / timezone)
# ---------------------------------------------------------------------------


class TestScheduleEvaluation:
    async def test_due_now_window_and_timezone(self, tmp_path) -> None:
        """A slot 20 min ago (inside the due window) is due-now; the
        timezone shift is honored (07:30 PDT = 14:30 UTC)."""
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [],
                    "timezone": "America/Los_Angeles",
                },
            )
            # 2026-10-27 (a Monday) 14:10 UTC == 07:10 PDT — the 07:30 slot
            # is 20 min in the future... use 14:40 UTC == 07:40 PDT instead
            # so the slot just passed, inside the due window.
            now = datetime(2026, 10, 27, 14, 40, tzinfo=UTC)
            evaluation = await _svc(registry).evaluate(now=now)
            assert len(evaluation["due_now"]) == 1
            slot = evaluation["due_now"][0]
            assert slot["routine_id"] == rid
            assert slot["time"] == "07:30"
            assert slot["state"] == "due"
            assert slot["timezone"] == "America/Los_Angeles"
            assert slot["at"] == "2026-10-27T14:30:00+00:00"
        finally:
            await engine.dispose()

    async def test_passed_unmet_beyond_due_window(self, tmp_path) -> None:
        """A slot more than DUE_WINDOW_MINUTES ago is passed-unmet (missed)."""
        engine, registry = await _make_harness(tmp_path)
        try:
            await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [],
                    "timezone": "America/Los_Angeles",
                },
            )
            # 08:00 PDT (15:00 UTC) — the 07:30 slot is 30 min... that is
            # still inside the default 45-min due window; use 16:30 UTC
            # (09:30 PDT) → 2 hours past → missed.
            now = datetime(2026, 10, 27, 16, 30, tzinfo=UTC)
            evaluation = await _svc(registry).evaluate(now=now)
            assert evaluation["due_now"] == []
            assert len(evaluation["missed_notes"]) == 1
            note = evaluation["missed_notes"][0]
            assert "no worries" in note["note"]
            assert DUE_WINDOW_MINUTES == 45  # the frozen window
        finally:
            await engine.dispose()

    async def test_weekday_boundary(self, tmp_path) -> None:
        """A routine scheduled for weekdays=[0] (Monday only) has NO slot
        on a Tuesday — even though the same wall-clock time exists."""
        engine, registry = await _make_harness(tmp_path)
        try:
            await _seed_routine(
                registry,
                kind="morning_briefing",
                title="Morning briefing",
                schedule={
                    "times": ["08:00"],
                    "weekdays": [0],
                    "timezone": "America/Los_Angeles",
                },
            )
            # 2026-10-26 is a Monday → the slot exists (08:00 PDT lapsed at
            # 10:00 PDT, well past the 45-min due window).
            mon = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)  # 10:00 PDT
            mon_eval = await _svc(registry).evaluate(now=mon)
            assert len(mon_eval["missed_notes"]) == 1

            # 2026-10-27 is a Tuesday → the Monday-only routine has nothing
            # to evaluate at all (no due, no missed, no upcoming).
            tue = datetime(2026, 10, 27, 17, 0, tzinfo=UTC)
            tue_eval = await _svc(registry).evaluate(now=tue)
            assert tue_eval["due_now"] == []
            assert tue_eval["missed_notes"] == []
            assert tue_eval["upcoming"] == []
        finally:
            await engine.dispose()

    async def test_empty_weekdays_means_every_day(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [],
                    "timezone": "America/Los_Angeles",
                },
            )
            rows = await _repo(registry).routines_due_on_local_day(
                date(2026, 10, 27), "America/Los_Angeles"
            )
            assert [r.id for r in rows] == [rid]
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 2. Missed window flagged EXACTLY once
# ---------------------------------------------------------------------------


class TestMissedWindowOnce:
    async def test_second_evaluation_does_not_reflag(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [],
                    "timezone": "America/Los_Angeles",
                },
            )
            now = datetime(2026, 10, 27, 16, 30, tzinfo=UTC)  # 09:30 PDT
            first = await _svc(registry).evaluate(now=now)
            assert len(first["missed_notes"]) == 1

            # Evaluate again at the same instant (same window) → the window
            # is already flagged, so NO second note and missed_flags holds
            # exactly one key.
            second = await _svc(registry).evaluate(now=now)
            assert second["missed_notes"] == []

            row = await _repo(registry).get_routine(
                first["missed_notes"][0]["routine_id"]
            )
            assert row is not None
            assert len(row.missed_flags) == 1
            key = next(iter(row.missed_flags))
            assert key.startswith(f"{row.id}:2026-10-27:07:30")
            # the marker value is a timestamp (one-time flag, never a count)
            datetime.fromisoformat(str(row.missed_flags[key]).replace("Z", "+00:00"))
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Wake fallback alarm (deterministic + persisted)
# ---------------------------------------------------------------------------


class TestWakeFallbackAlarm:
    async def test_alarm_persisted_in_deterministic_shape(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [],
                    "timezone": "America/Los_Angeles",
                },
            )
            # 05:00 PDT (12:00 UTC) — before the 07:30 slot.
            now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
            await _svc(registry).evaluate(now=now)

            row = await _repo(registry).get_routine(rid)
            assert row is not None
            alarm = row.deterministic_fallback_alarm
            assert alarm["kind"] == "clock_fallback"
            assert alarm["timezone"] == "America/Los_Angeles"
            assert alarm["trigger_level"] == 2
            at = datetime.fromisoformat(alarm["at"])
            local = at.astimezone(ZoneInfo("America/Los_Angeles"))
            assert (local.hour, local.minute) == (7, 30)
            assert local.date() == date(2026, 10, 27)

            # Determinism: the pure computation agrees with the persisted
            # alarm and is stable across calls.
            computed = compute_wake_fallback_alarm(row, now=now)
            assert computed is not None
            assert computed["at"] == alarm["at"]
            assert compute_wake_fallback_alarm(row, now=now) == computed
        finally:
            await engine.dispose()

    async def test_alarm_rolls_to_next_scheduled_day(self, tmp_path) -> None:
        """Past today's slot on a Monday-only schedule, the alarm rolls to
        the next Monday's 07:30 in the routine's timezone."""
        engine, registry = await _make_harness(tmp_path)
        try:
            await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [0],
                    "timezone": "America/Los_Angeles",
                },
            )
            # Monday 16:00 PDT (23:00 UTC) — today's 07:30 already passed.
            now = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)
            alarm = compute_wake_fallback_alarm(
                (await _repo(registry).list_routines(enabled_only=True))[0], now=now
            )
            assert alarm is not None
            local = datetime.fromisoformat(alarm["at"]).astimezone(
                ZoneInfo("America/Los_Angeles")
            )
            # next Monday is 2026-11-02; local slot 07:30 (PDT → UTC is PST
            # in November, but the LOCAL time is what must be 07:30 Monday)
            assert local.date() == date(2026, 11, 2)
            assert (local.hour, local.minute) == (7, 30)
        finally:
            await engine.dispose()

    async def test_no_alarm_for_non_wake_or_disabled(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            brief = await _seed_routine(
                registry,
                kind="morning_briefing",
                schedule={"times": ["08:00"], "weekdays": [], "timezone": "UTC"},
            )
            await _seed_routine(
                registry,
                title="Disabled wake",
                schedule={"times": ["07:00"], "weekdays": [], "timezone": "UTC"},
                enabled=False,
            )
            now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
            for row in await _repo(registry).list_routines(enabled_only=False):
                assert compute_wake_fallback_alarm(row, now=now) is None
            assert brief is not None
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 4. Sleep regularity (unknown when sparse)
# ---------------------------------------------------------------------------


class TestSleepRegularity:
    async def test_unknown_with_fewer_than_three_records(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            svc = _svc(registry)
            # 2 completed records (200 min apart bed/wake) → still unknown.
            base = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)
            for i in range(2):
                await svc.add_sleep_record(
                    bed_at=base + timedelta(hours=8 * i),
                    wake_at=base + timedelta(hours=8 * i) + timedelta(hours=7),
                    source="manual",
                )
            summary = await svc.sleep_summary(now=base + timedelta(hours=48))
            assert summary["regularity"] == "unknown"
            assert summary["n"] == 2
            assert summary["avg_minutes"] is None
            assert "unknown" in summary["description"]
        finally:
            await engine.dispose()

    async def test_not_unknown_with_enough_records(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            svc = _svc(registry)
            base = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
            for i in range(4):
                await svc.add_sleep_record(
                    bed_at=base + timedelta(hours=24 * i),
                    wake_at=base + timedelta(hours=24 * i) + timedelta(minutes=420),
                    source="manual",
                )
            summary = await svc.sleep_summary(now=base + timedelta(days=6))
            assert summary["n"] == 4
            assert summary["regularity"] != "unknown"
            assert summary["regularity"] == "stable"
            assert summary["avg_minutes"] == 420
        finally:
            await engine.dispose()

    async def test_zero_records_is_unknown(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            summary = await _svc(registry).sleep_summary()
            assert summary["regularity"] == "unknown"
            assert summary["n"] == 0
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Check-in idempotency
# ---------------------------------------------------------------------------


class TestCheckInIdempotency:
    async def test_same_day_twice_one_row(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(registry, kind="check_in", title="Evening check-in")
            svc = _svc(registry)
            day = datetime(2026, 10, 27, 21, 0, tzinfo=UTC)
            first = await svc.check_in(rid, at=day, completed=True, note="fine")
            second = await svc.check_in(
                rid, at=day + timedelta(minutes=30), completed=True, note="better"
            )
            # The upsert updated the single row in place (not a new row).
            assert first["id"] == second["id"]
            rows = await _repo(registry).list_check_ins(rid)
            assert len(rows) == 1
            assert rows[0].note == "better"
        finally:
            await engine.dispose()

    async def test_new_day_new_row(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(registry, kind="check_in")
            svc = _svc(registry)
            await svc.check_in(rid, at=datetime(2026, 10, 26, 21, 0, tzinfo=UTC))
            await svc.check_in(rid, at=datetime(2026, 10, 27, 21, 0, tzinfo=UTC))
            rows = await _repo(registry).list_check_ins(rid)
            assert len(rows) == 2
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 6. Complete clears the window flag
# ---------------------------------------------------------------------------


class TestCompleteClearsFlag:
    async def test_complete_clears_window_key(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(
                registry,
                schedule={
                    "times": ["07:30"],
                    "weekdays": [],
                    "timezone": "America/Los_Angeles",
                },
            )
            svc = _svc(registry)
            # Flag the window (as a missed evaluation would).
            await svc._repo.merge_missed_flags(
                rid, {f"{rid}:2026-10-27:07:30": "2026-10-27T16:30:00+00:00"}
            )
            row = await svc._repo.get_routine(rid)
            assert row is not None
            assert f"{rid}:2026-10-27:07:30" in (row.missed_flags or {})

            # Complete the routine the same local day → the window key is
            # cleared and the completion is stamped.
            completed = await svc.complete(
                rid, at=datetime(2026, 10, 27, 15, 0, tzinfo=UTC)  # 08:00 PDT
            )
            assert completed is not None
            assert completed["last_completed_at"] is not None
            row = await svc._repo.get_routine(rid)
            assert row is not None
            assert f"{rid}:2026-10-27:07:30" not in (row.missed_flags or {})

            # A completed window is not re-flagged by a later evaluation.
            now = datetime(2026, 10, 27, 16, 30, tzinfo=UTC)  # 09:30 PDT
            evaluation = await svc.evaluate(now=now)
            assert evaluation["missed_notes"] == []
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 7. Endpoint CRUD + degradation
# ---------------------------------------------------------------------------


def _wired_app(tmp_path: pathlib.Path, registry: RepositoryRegistry) -> FastAPI:
    from backend.api.endpoints import routines as r

    app = FastAPI()
    app.include_router(r.router)
    app.state.repository_registry = registry
    return app


class TestRoutinesHttp:
    async def test_degrades_without_registry(self, tmp_path) -> None:
        from backend.api.endpoints import routines as r

        app = FastAPI()
        app.include_router(r.router)
        app.state.repository_registry = None
        client = TestClient(app)

        assert client.get("/api/routines/").json() == {"data": [], "durable": False}
        today = client.get("/api/routines/today").json()
        assert today["durable"] is False
        assert today["sleep"]["regularity"] == "unknown"

        # Writes degrade to 202, executed: false (no fake success).
        created = client.post(
            "/api/routines/",
            json={"kind": "wake", "title": "Wake"},
        )
        assert created.status_code == 202
        assert created.json() == {"executed": False, "reason": "persistence_not_configured"}
        for path, payload in [
            ("/api/routines/nope/", {"title": "X"}),
            ("/api/routines/nope/checkin", {"completed": True}),
        ]:
            resp = client.request(
                "PATCH" if path.endswith("/") else "POST", path, json=payload
            )
            assert resp.status_code == 202
            assert resp.json()["executed"] is False
        assert (
            client.post("/api/routines/nope/complete").status_code == 202
        )

    async def test_crud_roundtrip(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            client = TestClient(_wired_app(tmp_path, registry))
            # POST (201) with a valid schedule.
            created = client.post(
                "/api/routines/",
                json={
                    "kind": "wake",
                    "title": "Wake up",
                    "schedule": {
                        "times": ["07:30"],
                        "weekdays": [0, 1, 2, 3, 4],
                        "timezone": "America/Los_Angeles",
                    },
                },
            )
            assert created.status_code == 201
            routine = created.json()["routine"]
            rid = routine["id"]
            assert routine["kind"] == "wake"
            assert routine["schedule"]["times"] == ["07:30"]
            assert routine["deterministic_fallback_alarm"] == {}
            assert routine["missed_flags"] == {}

            # GET list.
            listed = client.get("/api/routines/").json()
            assert listed["durable"] is True
            assert [r["id"] for r in listed["data"]] == [rid]

            # Invalid kind / schedule → 422.
            assert (
                client.post(
                    "/api/routines/",
                    json={"kind": "nope", "title": "X"},
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/routines/",
                    json={
                        "kind": "wake",
                        "title": "X",
                        "schedule": {"times": ["7:30"], "timezone": "UTC"},
                    },
                ).status_code
                == 422
            )

            # PATCH field-level (title only — schedule unchanged).
            patched = client.patch(f"/api/routines/{rid}", json={"title": "Rise"})
            assert patched.status_code == 200
            assert patched.json()["routine"]["title"] == "Rise"
            assert patched.json()["routine"]["schedule"]["times"] == ["07:30"]

            # PATCH unknown id → 404; checkin/complete on unknown → 404.
            assert client.patch("/api/routines/absent", json={"title": "X"}).status_code == 404
            assert client.post("/api/routines/absent/checkin", json={}).status_code == 404
            assert client.post("/api/routines/absent/complete").status_code == 404
        finally:
            await engine.dispose()

    async def test_checkin_and_complete_via_http(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(
                registry,
                kind="check_in",
                schedule={"times": ["21:00"], "weekdays": [], "timezone": "UTC"},
            )
            client = TestClient(_wired_app(tmp_path, registry))

            day = "2026-10-27T21:00:00Z"
            first = client.post(
                f"/api/routines/{rid}/checkin",
                json={"at": day, "completed": True, "note": "hi"},
            )
            assert first.status_code == 200
            assert first.json()["check_in"]["completed"] is True
            # Idempotent per day — a second same-day check-in is one row.
            second = client.post(
                f"/api/routines/{rid}/checkin",
                json={"at": "2026-10-27T21:30:00Z", "completed": True},
            )
            assert second.json()["check_in"]["id"] == first.json()["check_in"]["id"]

            completed = client.post(f"/api/routines/{rid}/complete")
            assert completed.status_code == 200
            assert completed.json()["routine"]["last_completed_at"] is not None
        finally:
            await engine.dispose()

    async def test_today_endpoint_shape(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            client = TestClient(_wired_app(tmp_path, registry))
            body = client.get("/api/routines/today").json()
            assert body["durable"] is True
            assert set(body) >= {
                "due_now",
                "upcoming",
                "missed_notes",
                "completed_today",
                "sleep",
            }
            assert body["sleep"]["regularity"] == "unknown"
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 8. Today provider + registration idempotency
# ---------------------------------------------------------------------------


class _FakeCtx:
    """A Today provider context (exposes .registry and .now)."""

    def __init__(self, registry: Any, now: datetime) -> None:
        self.registry = registry
        self.now = now


class TestRoutinesTodayProvider:
    async def test_coming_up_and_handled(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            rid = await _seed_routine(
                registry,
                kind="morning_briefing",
                title="Morning briefing",
                schedule={"times": ["08:00"], "weekdays": [], "timezone": "UTC"},
            )
            provider = RoutinesTodayProvider()
            # 06:00 UTC — the 08:00 slot is upcoming (within 48h).
            ctx = _FakeCtx(registry, datetime(2026, 10, 27, 6, 0, tzinfo=UTC))
            assert await provider.name() == "routines"
            assert await provider.needs_you(ctx) == []

            coming = await provider.coming_up(ctx)
            assert len(coming) == 1
            assert coming[0]["kind"] == "routine"
            assert coming[0]["routine_id"] == rid
            assert coming[0]["time"] == "08:00"
            assert coming[0]["at"] == "2026-10-27T08:00:00+00:00"

            handled = await provider.handled(ctx)
            assert handled == []

            # After a check-in, the provider reports it as handled.
            await _repo(registry).upsert_check_in(
                routine_id=rid, at=datetime(2026, 10, 27, 8, 5, tzinfo=UTC)
            )
            handled = await provider.handled(ctx)
            assert len(handled) == 1
            assert handled[0]["title"] == "Morning briefing"
            assert handled[0]["kind"] == "routine"
        finally:
            await engine.dispose()

    async def test_degrades_without_registry(self) -> None:
        provider = RoutinesTodayProvider()
        ctx = _FakeCtx(None, datetime(2026, 10, 27, 6, 0, tzinfo=UTC))
        assert await provider.coming_up(ctx) == []
        assert await provider.handled(ctx) == []
        assert await provider.needs_you(ctx) == []

    async def test_registration_is_idempotent(self) -> None:
        from backend.api.endpoints import today

        before = len(today._TODAY_PROVIDERS)
        p1 = register_routines_today_provider()
        p2 = register_routines_today_provider()
        # The aggregator dedupes by provider identity; each call registers
        # its own fresh instance, so the count grows by exactly one per
        # distinct instance — but re-registering the SAME instance is a no-op.
        assert p1 is not p2
        assert len(today._TODAY_PROVIDERS) == before + 2
        today.register_today_provider(p1)  # same instance → no-op
        assert len(today._TODAY_PROVIDERS) == before + 2
        # cleanup (the module-level list is process-global)
        today._TODAY_PROVIDERS[:] = today._TODAY_PROVIDERS[:before]


# ---------------------------------------------------------------------------
# 9. Wellness sleep seam
# ---------------------------------------------------------------------------


class TestWellnessSleepSeam:
    async def test_returns_iso_records_with_source(self, tmp_path) -> None:
        engine, registry = await _make_harness(tmp_path)
        try:
            svc = _svc(registry)
            await svc.add_sleep_record(
                bed_at=datetime(2026, 10, 26, 22, 30, tzinfo=UTC),
                wake_at=datetime(2026, 10, 27, 6, 15, tzinfo=UTC),
                source="manual",
            )
            await svc.add_sleep_record(
                bed_at=datetime(2026, 10, 25, 23, 0, tzinfo=UTC),
                source="apple_health",
            )
            provider = make_wellness_sleep_provider(registry)
            records = await provider(14)
            assert len(records) == 2
            for rec in records:
                assert set(rec) == {"bed_at", "wake_at", "duration_minutes", "source"}
                # ISO strings (parseable), wake_at may be None.
                datetime.fromisoformat(rec["bed_at"])
                if rec["wake_at"] is not None:
                    datetime.fromisoformat(rec["wake_at"])
            by_source = {rec["source"] for rec in records}
            assert by_source == {"manual", "apple_health"}
            manual = next(r for r in records if r["source"] == "manual")
            assert manual["duration_minutes"] == 465
            apple = next(r for r in records if r["source"] == "apple_health")
            assert apple["wake_at"] is None
            assert apple["duration_minutes"] is None
        finally:
            await engine.dispose()

    async def test_degraded_returns_empty_list(self) -> None:
        provider = make_wellness_sleep_provider(None)
        assert await provider(14) == []
        assert await provider(1) == []
