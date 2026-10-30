"""P2-A — unified obligation plane (Personal Reliability).

Covers the frozen contract (``docs/contracts/obligation.md``):

1. **Dedup key** — the canonical grammar
   ``{type}|{plane}:{record_id}|{YYYY-MM-DD}``; an identical bill seen via
   two different bridge calls produces one row (evidence merged, no second
   row).
2. **Bridge** — a bill dict / calendar event / follow-up becomes an
   obligation spec with the right type, dedup key, ``source_record_ref``,
   and evidence.
3. **Create + read** — explicit create, list with status/type/actionable
   filters, get by id, 404 for unknown.
4. **Status transitions** — accept (inferred → explicit/active), snooze
   (reversible, wakes when the window passes), complete (terminal done),
   dismiss (terminal dismissed), reverify (stale → active), stale sweep
   (unverified rows older than the threshold become stale).
5. **Needs You / Coming Up / Waiting On** — projections: actionable
   excludes terminal + snoozed; coming-up window; waiting-on filter.
6. **Low Energy Mode 2.0** — ``pick_next_action`` returns one small next
   step; ``urgent_only`` keeps only priority ≥ 3; nothing actionable →
   ``None``.
7. **API degradation** — no registry → empty shapes / 202 no-op.

The durable plane is exercised on a SQLite scratch DB through the real
``RepositoryRegistry`` on ``app.state`` (the
``tests/backend/test_bills.py`` harness pattern).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.obligations.service import (
    ObligationsService,
    build_obligation_from_bill,
    build_obligation_from_appointment,
    build_obligation_from_followup,
    dedup_key_for,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.obligations import ObligationRow
from backend.persistence.models.personal import AttentionItemRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.obligations_repo import (
    ObligationsRepository,
    _merge_evidence,
)

# ---------------------------------------------------------------------------
# Shared harness (pattern: tests/backend/test_bills.py)
# ---------------------------------------------------------------------------


async def _make_harness(
    tmp_path: pathlib.Path,
) -> tuple[AsyncEngine, RepositoryRegistry]:
    """A durable SQLite DB + repository registry, wired like production."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'obligations.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            ObligationRow.__table__.metadata.create_all,
            tables=[ObligationRow.__table__, AttentionItemRow.__table__],
        )
    registry = RepositoryRegistry(make_session_factory(engine))
    return engine, registry


def _wired_app(registry: RepositoryRegistry) -> FastAPI:
    from backend.api.endpoints import obligations as o

    app = FastAPI()
    app.include_router(o.router)
    app.state.repository_registry = registry
    return app


def _degraded_app() -> FastAPI:
    from backend.api.endpoints import obligations as o

    app = FastAPI()
    app.include_router(o.router)
    # no repository_registry on app.state -> degraded
    return app


def _svc(registry: RepositoryRegistry) -> ObligationsService:
    return ObligationsService(ObligationsRepository(registry.session_factory))


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# 1. Dedup key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("otype", "src", "due", "expected"),
    [
        ("bill", {"plane": "bills", "id": "a1"}, datetime(2026, 11, 15, 12, 0, tzinfo=UTC), "bill|bills:a1|2026-11-15"),
        ("bill", {"plane": "bills", "id": "a1"}, datetime(2026, 11, 15, 23, 59, tzinfo=UTC), "bill|bills:a1|2026-11-15"),  # same day
        ("appointment", {"plane": "calendar", "id": "e4"}, datetime(2026, 11, 20, tzinfo=UTC), "appointment|calendar:e4|2026-11-20"),
        ("follow_up", {"plane": "communications", "id": "h7"}, None, "follow_up|communications:h7|nodate"),
    ],
)
def test_dedup_key_grammar(otype, src, due, expected):
    assert dedup_key_for(obligation_type=otype, source_ref=src, due_at=due) == expected


def test_dedup_key_same_day_collapses():
    a = dedup_key_for(obligation_type="bill", source_ref={"plane": "bills", "id": "x"}, due_at=datetime(2026, 1, 1, 1, 0, tzinfo=UTC))
    b = dedup_key_for(obligation_type="bill", source_ref={"plane": "bills", "id": "x"}, due_at=datetime(2026, 1, 1, 18, 30, tzinfo=UTC))
    assert a == b


def test_dedup_key_diff_day_differs():
    a = dedup_key_for(obligation_type="bill", source_ref={"plane": "bills", "id": "x"}, due_at=datetime(2026, 1, 1, tzinfo=UTC))
    b = dedup_key_for(obligation_type="bill", source_ref={"plane": "bills", "id": "x"}, due_at=datetime(2026, 1, 2, tzinfo=UTC))
    assert a != b


def test_dedup_key_rejects_unknown_type():
    with pytest.raises(ValueError):
        dedup_key_for(obligation_type="not_a_type", source_ref=None)


def test_merge_evidence_no_duplicate():
    existing = [{"source_kind": "email", "source_id": "m1", "snippet": "pay rent", "confidence": 0.9}]
    incoming = [
        {"source_kind": "email", "source_id": "m1", "snippet": "pay rent (updated)", "confidence": 0.95},
        {"source_kind": "calendar", "source_id": "e1", "snippet": "rent due", "confidence": 0.8},
    ]
    merged = _merge_evidence(existing, incoming)
    assert len(merged) == 2  # no duplicate on (email, m1)
    assert merged[0]["snippet"] == "pay rent (updated)"  # in-place update
    assert merged[1]["source_kind"] == "calendar"


# ---------------------------------------------------------------------------
# 2. Bridge
# ---------------------------------------------------------------------------


def test_bridge_bill():
    spec = build_obligation_from_bill(
        {
            "id": "b1",
            "payee": "Electric Company",
            "amount_cents": "1234",
            "currency": "USD",
            "due_at": _iso(_now() + timedelta(days=3)),
            "status": "pending",
            "recurrence": "monthly",
            "evidence": [{"source_kind": "email", "source_id": "m9"}],
        }
    )
    assert spec["obligation_type"] == "bill"
    assert spec["dedup_key"].startswith("bill|bills:b1|")
    assert spec["source_record_ref"] == {"plane": "bills", "id": "b1"}
    assert spec["origin"] == "explicit"
    assert spec["priority"] in (1, 2)  # within 7 days / 48h
    assert spec["relationships"] == [{"kind": "recurrence", "target_id": "b1"}]
    assert spec["evidence"] == [{"source_kind": "email", "source_id": "m9"}]


def test_bridge_bill_overdue_priority_3():
    spec = build_obligation_from_bill(
        {
            "id": "b2",
            "payee": "Water",
            "amount_cents": "100",
            "currency": "USD",
            "due_at": _iso(_now() - timedelta(days=2)),
            "status": "pending",
            "recurrence": "one-time",
        }
    )
    assert spec["priority"] == 3


def test_bridge_appointment_with_prep_and_docs():
    spec = build_obligation_from_appointment(
        {
            "id": "ev1",
            "title": "Dentist",
            "start": _iso(_now() + timedelta(days=5)),
            "prep": [{"id": "p1", "title": "Book time off"}],
            "required_docs": [{"id": "d1", "title": "Insurance card"}],
        }
    )
    assert spec["obligation_type"] == "appointment"
    assert spec["dedup_key"].startswith("appointment|calendar:ev1|")
    assert spec["next_action"] == "Prepare: Book time off"
    assert spec["relationships"] == [
        {"kind": "prep_task", "target_id": "p1"},
        {"kind": "required_doc", "target_id": "d1"},
    ]


def test_bridge_followup_high_urgency():
    spec = build_obligation_from_followup(
        {
            "id": "f1",
            "title": "Reply to boss",
            "due_at": _iso(_now() + timedelta(days=1)),
            "urgency": "high",
        }
    )
    assert spec["obligation_type"] == "follow_up"
    assert spec["priority"] == 2
    assert spec["dedup_key"].startswith("follow_up|communications:f1|")


# ---------------------------------------------------------------------------
# 3–6. Durable behaviour (SQLite scratch DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_read(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        out = await svc.create(
            obligation_type="task",
            title="File taxes",
            origin="explicit",
            due_at=_now() + timedelta(days=10),
            next_action="Gather W-2s",
            priority=2,
        )
        assert out["status"] == "active"
        assert out["origin"] == "explicit"
        got = await svc.get(out["id"])
        assert got["title"] == "File taxes"
        # unknown id
        assert await svc.get("nope") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dedup_upsert_merges_no_second_row(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        src = {
            "plane": "bills",
            "record": {
                "id": "b9",
                "payee": "Internet",
                "amount_cents": "8000",
                "currency": "USD",
                "due_at": _iso(_now() + timedelta(days=5)),
                "status": "pending",
                "recurrence": "monthly",
                "evidence": [{"source_kind": "email", "source_id": "m1"}],
            },
        }
        first = await svc.upsert_from_source(src)
        assert first["created"] is True
        # second observation from a DIFFERENT source (calendar) → same key
        src2 = {
            "plane": "bills",
            "record": {
                "id": "b9",
                "payee": "Internet",
                "amount_cents": "8000",
                "currency": "USD",
                "due_at": _iso(_now() + timedelta(days=5)),
                "status": "pending",
                "recurrence": "monthly",
                "evidence": [{"source_kind": "calendar", "source_id": "e1"}],
            },
        }
        second = await svc.upsert_from_source(src2)
        assert second["created"] is False
        assert second["id"] == first["id"]
        assert len(second["evidence"]) == 2  # merged
        rows = await svc._repo.list(limit=10)
        assert len(rows) == 1  # no second row
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_accept_inferred_becomes_actionable(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        out = await svc.create(obligation_type="commitment", title="Call plumber", origin="inferred", priority=2)
        # needs_you includes it but flagged
        needs = await svc.needs_you()
        assert any(d["id"] == out["id"] and d["requires_acceptance"] for d in needs)
        # accept
        accepted = await svc.accept(out["id"])
        assert accepted["origin"] == "explicit"
        needs2 = await svc.needs_you()
        assert not any(d["id"] == out["id"] and d["requires_acceptance"] for d in needs2)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snooze_reversible_and_wakes(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        out = await svc.create(obligation_type="task", title="Renew license", origin="explicit")
        until = _now() + timedelta(minutes=5)
        snoozed = await svc.snooze(out["id"], until)
        assert snoozed["status"] == "snoozed"
        # snoozed excluded from needs_you
        assert all(d["id"] != out["id"] for d in await svc.needs_you())
        # before window: no wake
        woke = await svc.wake_up_snoozed(_now() + timedelta(minutes=1))
        assert not any(r.id == out["id"] for r in woke)
        # after window: wakes to active
        woke = await svc.wake_up_snoozed(_now() + timedelta(minutes=6))
        assert any(r["id"] == out["id"] and r["status"] == "active" for r in woke)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_and_dismiss_terminal(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        a = await svc.create(obligation_type="task", title="A", origin="explicit")
        b = await svc.create(obligation_type="task", title="B", origin="explicit")
        done = await svc.complete(a["id"])
        assert done["status"] == "done" and done["completed_at"]
        dis = await svc.dismiss(b["id"])
        assert dis["status"] == "dismissed" and dis["dismissed_at"]
        # terminal excluded from needs_you
        needs = await svc.needs_you()
        ids = {d["id"] for d in needs}
        assert a["id"] not in ids and b["id"] not in ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_sweep_and_reverify(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        old = _now() - timedelta(days=45)
        row = await svc._repo.create(
            obligation_type="bill",
            dedup_key="bill|bills:old|2026-01-01",
            title="Old bill",
            origin="explicit",
            last_verified_at=old,
        )
        fresh = await svc.create(obligation_type="bill", title="Fresh", origin="explicit")
        stale = await svc.sweep_stale()
        ids = {d["id"] for d in stale}
        assert row.id in ids
        assert fresh["id"] not in ids
        # reverify flips stale → active
        rever = await svc.reverify(row.id)
        assert rever["status"] == "active"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_needs_you_coming_up_waiting_on(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        svc = _svc(registry)
        soon = await svc.create(obligation_type="bill", title="Soon bill", origin="explicit", due_at=_now() + timedelta(days=2), priority=3)
        later = await svc.create(obligation_type="task", title="Later", origin="explicit", due_at=_now() + timedelta(days=60))
        waiting = await svc.create(obligation_type="waiting_on", title="Waiting for refund", origin="explicit")
        needs = await svc.needs_you()
        assert any(d["id"] == soon["id"] for d in needs)
        coming = await svc.coming_up(days=14)
        assert any(d["id"] == soon["id"] for d in coming)
        assert not any(d["id"] == later["id"] for d in coming)
        w = await svc.waiting_on()
        assert any(d["id"] == waiting["id"] for d in w)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Low Energy Mode 2.0
# ---------------------------------------------------------------------------


def _needs_fixture() -> list[dict[str, Any]]:
    now = _now()
    return [
        {
            "id": "o-urgent",
            "title": "Pay overdue bill",
            "status": "active",
            "origin": "explicit",
            "requires_acceptance": False,
            "next_action": "Pay the $80 internet bill before it is cut off",
            "priority": 3,
            "due_at": _iso(now - timedelta(hours=6)),
        },
        {
            "id": "o-normal",
            "title": "File taxes",
            "status": "active",
            "origin": "explicit",
            "requires_acceptance": False,
            "next_action": "Gather W-2s",
            "priority": 1,
            "due_at": _iso(now + timedelta(days=10)),
        },
        {
            "id": "o-inferred",
            "title": "Inferred thing",
            "status": "active",
            "origin": "inferred",
            "requires_acceptance": True,
            "next_action": "Review inferred item",
            "priority": 2,
            "due_at": _iso(now + timedelta(days=1)),
        },
    ]


def test_low_energy_picks_most_urgent():
    svc = _svc_for_lem()
    pick = svc.pick_next_action(_needs_fixture())
    assert pick is not None
    assert pick["obligation_id"] == "o-urgent"
    assert pick["tiny_step"] == "Pay the $80 internet bill before it is cut off"
    assert pick["urgency"] == 3
    assert "done" in pick["done_later_help_me"] and "later" in pick["done_later_help_me"] and "help_me" in pick["done_later_help_me"]


def test_low_energy_urgent_only_excludes_normal():
    svc = _svc_for_lem()
    pick = svc.pick_next_action(_needs_fixture(), urgent_only=True)
    assert pick is not None
    assert pick["obligation_id"] == "o-urgent"
    # remove the urgent one → urgent_only returns None (normal + inferred excluded)
    items = [d for d in _needs_fixture() if d["id"] != "o-urgent"]
    assert svc.pick_next_action(items, urgent_only=True) is None


def test_low_energy_no_actionable_returns_none():
    svc = _svc_for_lem()
    assert svc.pick_next_action([]) is None
    # only inferred → excluded → None
    inferred_only = [d for d in _needs_fixture() if d["id"] == "o-inferred"]
    assert svc.pick_next_action(inferred_only) is None


def _svc_for_lem() -> ObligationsService:
    # pick_next_action is a pure function; use a service without a repo.
    return ObligationsService.__new__(ObligationsService)


# ---------------------------------------------------------------------------
# API surface (HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_degraded(tmp_path):
    client = TestClient(_degraded_app())
    r = client.get("/api/obligations/")
    assert r.status_code == 200 and r.json() == {"obligations": []}
    r = client.get("/api/obligations/needs-you")
    assert r.status_code == 200 and r.json() == {"needs_you": []}
    r = client.post("/api/obligations/", json={"obligation_type": "task", "title": "x"})
    assert r.status_code == 202 and r.json()["executed"] is False


@pytest.mark.asyncio
async def test_api_create_list_complete(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        client = TestClient(_wired_app(registry))
        r = client.post(
            "/api/obligations/",
            json={
                "obligation_type": "task",
                "title": "Renew passport",
                "due_at": _iso(_now() + timedelta(days=30)),
                "next_action": "Book an appointment",
                "priority": 2,
            },
        )
        assert r.status_code == 201, r.text
        oid = r.json()["id"]
        r = client.get("/api/obligations/")
        assert r.status_code == 200
        assert any(o["id"] == oid for o in r.json()["obligations"])
        r = client.post(f"/api/obligations/{oid}/complete")
        assert r.status_code == 200 and r.json()["obligation"]["status"] == "done"
        # 404 for unknown
        r = client.post("/api/obligations/nope/complete")
        assert r.status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_bridge_and_snooze(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        client = TestClient(_wired_app(registry))
        r = client.post(
            "/api/obligations/bridge",
            json={
                "plane": "bills",
                "record": {
                    "id": "bX",
                    "payee": "Gas",
                    "amount_cents": "6500",
                    "currency": "USD",
                    "due_at": _iso(_now() + timedelta(days=4)),
                    "status": "pending",
                    "recurrence": "monthly",
                },
            },
        )
        assert r.status_code == 201, r.text
        oid = r.json()["id"]
        # snooze
        r = client.post(
            f"/api/obligations/{oid}/snooze",
            params={"until": _iso(_now() + timedelta(days=1))},
        )
        assert r.status_code == 200 and r.json()["obligation"]["status"] == "snoozed"
        # reverify flips a stale row
        svc = _svc(registry)
        await svc._repo.mark_stale(threshold_days=0.0001)
        r = client.post(f"/api/obligations/{oid}/reverify")
        assert r.status_code == 200
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_invalid_type_422(tmp_path):
    engine, registry = await _make_harness(tmp_path)
    try:
        client = TestClient(_wired_app(registry))
        r = client.post("/api/obligations/", json={"obligation_type": "bogus", "title": "x"})
        assert r.status_code == 422
    finally:
        await engine.dispose()
