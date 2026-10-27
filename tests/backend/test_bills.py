"""Workstream G2 — bills REST surface (Personal-Ops Recovery §7).

Covers the frozen §15 route list for ``/api/bills``:

1. **Create** — ``POST /`` returns 201, status always ``pending``
   (status is never a create input); ``yearly`` maps to the durable
   contract (``other`` + ``{"years": 1}``); validation errors → 422.
2. **List** — ``GET /?status=`` filters by status; every payload carries
   ``escalation_level``.
3. **Upcoming** — ``GET /upcoming?days=`` returns bills due within the
   window (confirmed_paid excluded; overdue-unconfirmed are swept to
   ``stale`` first and excluded from the window).
4. **Calendar** — ``GET /calendar?month=YYYY-MM`` groups obligations by
   day; invalid month → 422.
5. **Confirm-paid** — the ONLY paid-state writer: flips the bill to
   ``confirmed_paid`` AND resolves the durable attention item that
   references ``bill:{id}`` (if one exists); unknown bill → 404.
6. **PATCH** — may set ``status`` to ``pending`` or ``disputed``;
   ``confirmed_paid`` / ``stale`` via PATCH → 422 (the paid state has
   exactly one writer; ``stale`` is owned by the sweep); unknown bill → 404.
7. **Stale sweep** — a side effect of ``GET /`` and ``GET /upcoming``:
   an overdue, unconfirmed bill becomes ``stale`` (never
   ``confirmed_paid``); disputed/confirmed rows are untouched.
8. **Escalation mirroring** — a pending bill due in 3 days (level 1)
   creates exactly ONE durable attention item; a second read refreshes
   it in place (still one row, state active, level tracked in
   ``escalation_policy``); a level-0 bill creates nothing; confirming
   the bill resolves the item.
9. **House degradation** — with no ``repository_registry`` on
   ``app.state``: GETs return 200 with empty shapes, writes return 202
   ``{"executed": false, "reason": "persistence_not_configured"}``.

The durable plane is exercised on a SQLite scratch DB through the real
``RepositoryRegistry`` on ``app.state`` (the
``tests/backend/test_wellness.py`` harness pattern); the HTTP surface
through ``TestClient``.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.connection import make_session_factory
from backend.persistence.models.bills import BillRow
from backend.persistence.models.personal import AttentionItemRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.bills_repo import BillsRepository

# ---------------------------------------------------------------------------
# Shared harness (pattern: tests/backend/test_wellness.py)
# ---------------------------------------------------------------------------


async def _make_harness(tmp_path: pathlib.Path) -> tuple[AsyncEngine, RepositoryRegistry]:
    """A durable SQLite DB + repository registry, wired like production."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'bills.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            BillRow.__table__.metadata.create_all,
            tables=[BillRow.__table__, AttentionItemRow.__table__],
        )
    registry = RepositoryRegistry(make_session_factory(engine))
    return engine, registry


def _wired_app(registry: RepositoryRegistry) -> FastAPI:
    from backend.api.endpoints import bills as b

    app = FastAPI()
    app.include_router(b.router)
    app.state.repository_registry = registry
    return app


def _degraded_app() -> FastAPI:
    """No ``repository_registry`` attribute at all → the degrade paths."""
    from backend.api.endpoints import bills as b

    app = FastAPI()
    app.include_router(b.router)
    return app


async def _seed_bill(
    registry: RepositoryRegistry,
    *,
    payee: str = "Acme Power",
    amount_cents: str = "14250",
    currency: str = "USD",
    due_in_days: float = 3.0,
    status: str = "pending",
    recurrence: str = "one-time",
) -> dict[str, Any]:
    """Create a bill through the repository (relative to the real clock)."""
    repo = BillsRepository(registry.session_factory)
    row = await repo.create_bill(
        payee=payee,
        amount_cents=amount_cents,
        currency=currency,
        due_at=datetime.now(UTC) + timedelta(days=due_in_days),
        recurrence=recurrence,
        status=status,
    )
    return {
        "id": row.id,
        "payee": row.payee,
        "amount_cents": row.amount_cents,
        "status": row.status,
        "due_at": row.due_at,
    }


async def _seed_attention_item(
    registry: RepositoryRegistry, bill_id: str, *, level: int = 1
) -> str:
    """A durable attention item that references ``bill:{id}``."""
    row = await registry.attention.create_attention_item(
        title=f"Bill escalation {level}: seeded",
        urgency="normal",
        source_refs=[f"bill:{bill_id}"],
        state="active",
    )
    return row.id


async def _attention(
    registry: RepositoryRegistry, *, state: str | None = None
) -> list[AttentionItemRow]:
    return await registry.attention.list_attention_items(state=state)


# ---------------------------------------------------------------------------
# 1. Create
# ---------------------------------------------------------------------------


class TestCreateBill:
    async def test_create_returns_201_with_pending_status(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        r = client.post(
            "/api/bills/",
            json={
                "payee": "Acme Power",
                "amount_cents": 14250,
                "currency": "USD",
                "due_at": "2027-01-15T12:00:00+00:00",
                "recurrence": "monthly",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["executed"] is True
        bill = body["bill"]
        assert bill["status"] == "pending"  # always pending on create
        assert bill["confirmed_at"] is None
        assert bill["payee"] == "Acme Power"
        assert bill["amount_cents"] == "14250"
        assert bill["currency"] == "USD"
        assert bill["recurrence"] == "monthly"
        assert bill["escalation_level"] == 0  # far future → level 0
        await engine.dispose()

    async def test_create_yearly_maps_to_durable_contract(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        r = client.post(
            "/api/bills/",
            json={
                "payee": "Dental Insurance",
                "amount_cents": 9000,
                "due_at": "2027-10-01T00:00:00Z",
                "recurrence": "yearly",
            },
        )
        assert r.status_code == 201
        bill = r.json()["bill"]
        # "yearly" is carried as "other" + {"years": 1} per the durable contract.
        assert bill["recurrence"] == "other"
        assert bill["interval"] == {"years": 1}
        assert bill["status"] == "pending"
        await engine.dispose()

    async def test_create_missing_payee_is_422(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        r = client.post(
            "/api/bills/",
            json={"amount_cents": 100, "due_at": "2027-01-01T00:00:00Z"},
        )
        assert r.status_code == 422
        await engine.dispose()

    async def test_create_negative_amount_is_422(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        r = client.post(
            "/api/bills/",
            json={
                "payee": "Acme",
                "amount_cents": -5,
                "due_at": "2027-01-01T00:00:00Z",
            },
        )
        assert r.status_code == 422
        await engine.dispose()

    async def test_create_bad_recurrence_is_422(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        r = client.post(
            "/api/bills/",
            json={
                "payee": "Acme",
                "amount_cents": 100,
                "due_at": "2027-01-01T00:00:00Z",
                "recurrence": "biweekly",
            },
        )
        assert r.status_code == 422
        await engine.dispose()


# ---------------------------------------------------------------------------
# 2. List with status filter
# ---------------------------------------------------------------------------


class TestListBills:
    async def test_list_filters_by_status(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        await _seed_bill(registry, payee="A", due_in_days=30)
        await _seed_bill(registry, payee="B", due_in_days=20, status="disputed")
        # An overdue pending bill → swept to stale on the first read.
        await _seed_bill(registry, payee="C", due_in_days=-5)
        client = TestClient(_wired_app(registry))

        all_bills = client.get("/api/bills/").json()["bills"]
        assert len(all_bills) == 3
        for bill in all_bills:
            assert "escalation_level" in bill  # present on every payload

        pending = client.get("/api/bills/?status=pending").json()["bills"]
        assert [b["payee"] for b in pending] == ["A"]
        assert pending[0]["escalation_level"] == 0  # 30 days out

        disputed = client.get("/api/bills/?status=disputed").json()["bills"]
        assert [b["payee"] for b in disputed] == ["B"]
        assert disputed[0]["escalation_level"] == 1  # frozen at level 1

        stale = client.get("/api/bills/?status=stale").json()["bills"]
        assert [b["payee"] for b in stale] == ["C"]
        assert stale[0]["escalation_level"] == 3  # stale behaves as level 3
        assert stale[0]["confirmed_at"] is None  # never auto-confirmed

        paid = client.get("/api/bills/?status=confirmed_paid").json()["bills"]
        assert paid == []
        await engine.dispose()

    async def test_list_empty_returns_durable_shape(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        assert client.get("/api/bills/").json() == {"bills": [], "durable": True}
        await engine.dispose()

    async def test_list_invalid_status_is_422(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        assert client.get("/api/bills/?status=maybe").status_code == 422
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Upcoming
# ---------------------------------------------------------------------------


class TestUpcoming:
    async def test_upcoming_window_excludes_paid_far_and_past(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        await _seed_bill(registry, payee="Soon", due_in_days=3)
        await _seed_bill(registry, payee="SoonPaid", due_in_days=2, status="confirmed_paid")
        await _seed_bill(registry, payee="Far", due_in_days=40)
        await _seed_bill(registry, payee="Past", due_in_days=-1)  # → stale on read
        client = TestClient(_wired_app(registry))

        body = client.get("/api/bills/upcoming?days=14").json()
        assert [b["payee"] for b in body["bills"]] == ["Soon"]
        assert body["days"] == 14
        assert body["durable"] is True
        assert body["bills"][0]["escalation_level"] == 1  # due within 7 days
        await engine.dispose()

    async def test_upcoming_respects_custom_days(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        await _seed_bill(registry, payee="At30", due_in_days=30)
        client = TestClient(_wired_app(registry))

        assert client.get("/api/bills/upcoming?days=14").json()["bills"] == []
        payees = [b["payee"] for b in client.get("/api/bills/upcoming?days=45").json()["bills"]]
        assert payees == ["At30"]
        await engine.dispose()


# ---------------------------------------------------------------------------
# 4. Monthly calendar
# ---------------------------------------------------------------------------


class TestCalendar:
    async def test_calendar_groups_obligations_by_day(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        repo = BillsRepository(registry.session_factory)
        # Two obligations on one day, one on another, one outside the month.
        await repo.create_bill(
            payee="Rent",
            amount_cents=180000,
            currency="USD",
            due_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            recurrence="monthly",
        )
        await repo.create_bill(
            payee="Power",
            amount_cents=14250,
            currency="USD",
            due_at=datetime(2026, 11, 1, 18, 0, tzinfo=UTC),
            recurrence="monthly",
        )
        await repo.create_bill(
            payee="Internet",
            amount_cents=7000,
            currency="USD",
            due_at=datetime(2026, 11, 15, tzinfo=UTC),
            recurrence="monthly",
        )
        await repo.create_bill(
            payee="Outside",
            amount_cents=1000,
            currency="USD",
            due_at=datetime(2026, 12, 20, tzinfo=UTC),
            recurrence="monthly",
        )
        client = TestClient(_wired_app(registry))

        body = client.get("/api/bills/calendar?month=2026-11").json()
        assert body["month"] == "2026-11"
        assert body["durable"] is True
        assert [d["date"] for d in body["days"]] == ["2026-11-01", "2026-11-15"]
        assert {o["payee"] for o in body["days"][0]["obligations"]} == {"Rent", "Power"}
        for obligation in body["days"][0]["obligations"]:
            assert set(obligation) == {"bill_id", "payee", "amount_cents", "currency", "status"}
        assert [d["obligations"][0]["payee"] for d in body["days"]][1] == "Internet"
        await engine.dispose()

    async def test_calendar_empty_month(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        body = client.get("/api/bills/calendar?month=2026-01").json()
        assert body == {
            "month": "2026-01",
            "days": [],
            "num_days": 31,
            "durable": True,
        }
        await engine.dispose()

    async def test_calendar_invalid_month_is_422(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        assert client.get("/api/bills/calendar?month=2026-13").status_code == 422
        assert client.get("/api/bills/calendar?month=nov2026").status_code == 422
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Confirm-paid (the ONLY paid-state writer) + attention resolution
# ---------------------------------------------------------------------------


class TestConfirmPaid:
    async def test_confirm_paid_flips_status_and_resolves_durable_attention(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="Acme Power", due_in_days=3)
        item_id = await _seed_attention_item(registry, bill["id"], level=1)
        client = TestClient(_wired_app(registry))

        r = client.post(f"/api/bills/{bill['id']}/confirm-paid")
        assert r.status_code == 200
        body = r.json()
        assert body["executed"] is True
        assert body["bill"]["status"] == "confirmed_paid"
        assert body["bill"]["confirmed_at"] is not None
        assert body["bill"]["escalation_level"] is None  # no escalation once paid

        # The durable attention item referencing this bill was resolved.
        rows = await _attention(registry)
        assert len(rows) == 1
        assert rows[0].id == item_id
        assert rows[0].state == "resolved"
        assert rows[0].resolved_at is not None
        await engine.dispose()

    async def test_confirm_paid_unknown_bill_is_404(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        assert client.post("/api/bills/does-not-exist/confirm-paid").status_code == 404
        await engine.dispose()

    async def test_confirm_paid_is_idempotent(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="X", due_in_days=5)
        client = TestClient(_wired_app(registry))

        assert client.post(f"/api/bills/{bill['id']}/confirm-paid").status_code == 200
        r2 = client.post(f"/api/bills/{bill['id']}/confirm-paid")
        assert r2.status_code == 200
        assert r2.json()["bill"]["status"] == "confirmed_paid"
        await engine.dispose()


# ---------------------------------------------------------------------------
# 6. PATCH — status constraints
# ---------------------------------------------------------------------------


class TestPatchBill:
    async def test_patch_can_set_disputed(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="Contested", due_in_days=30)
        client = TestClient(_wired_app(registry))

        r = client.patch(f"/api/bills/{bill['id']}", json={"status": "disputed"})
        assert r.status_code == 200
        assert r.json()["executed"] is True
        assert r.json()["bill"]["status"] == "disputed"
        assert r.json()["bill"]["escalation_level"] == 1  # frozen at level 1

        # And back to pending.
        r2 = client.patch(f"/api/bills/{bill['id']}", json={"status": "pending"})
        assert r2.status_code == 200
        assert r2.json()["bill"]["status"] == "pending"
        await engine.dispose()

    async def test_patch_cannot_set_confirmed_paid(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="NoCheats", due_in_days=30)
        client = TestClient(_wired_app(registry))

        r = client.patch(f"/api/bills/{bill['id']}", json={"status": "confirmed_paid"})
        assert r.status_code == 422
        # The bill must NOT have been flipped by the rejected PATCH.
        pending = client.get("/api/bills/?status=pending").json()["bills"]
        assert [b["id"] for b in pending] == [bill["id"]]
        await engine.dispose()

    async def test_patch_cannot_set_stale(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="NoStale", due_in_days=30)
        client = TestClient(_wired_app(registry))

        r = client.patch(f"/api/bills/{bill['id']}", json={"status": "stale"})
        assert r.status_code == 422
        pending = client.get("/api/bills/?status=pending").json()["bills"]
        assert [b["id"] for b in pending] == [bill["id"]]
        await engine.dispose()

    async def test_patch_field_updates(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="Old Payee", due_in_days=30)
        client = TestClient(_wired_app(registry))

        r = client.patch(
            f"/api/bills/{bill['id']}",
            json={"payee": "New Payee", "amount_cents": 9999, "currency": "EUR"},
        )
        assert r.status_code == 200
        updated = r.json()["bill"]
        assert updated["payee"] == "New Payee"
        assert updated["amount_cents"] == "9999"
        assert updated["currency"] == "EUR"
        # Untouched fields keep their values (partial-patch semantics).
        assert updated["status"] == "pending"
        await engine.dispose()

    async def test_patch_empty_body_is_422(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="Empty", due_in_days=30)
        client = TestClient(_wired_app(registry))

        assert client.patch(f"/api/bills/{bill['id']}", json={}).status_code == 422
        await engine.dispose()

    async def test_patch_unknown_bill_is_404(self, tmp_path: pathlib.Path) -> None:
        engine, registry = await _make_harness(tmp_path)
        client = TestClient(_wired_app(registry))

        assert client.patch("/api/bills/nope", json={"payee": "X"}).status_code == 404
        await engine.dispose()


# ---------------------------------------------------------------------------
# 7. Stale sweep (never confirms)
# ---------------------------------------------------------------------------


class TestStaleSweep:
    async def test_sweep_marks_overdue_pending_stale_not_confirmed(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="Overdue", due_in_days=-10)
        client = TestClient(_wired_app(registry))

        # The sweep runs as a side effect of GET / (and GET /upcoming).
        bills = client.get("/api/bills/").json()["bills"]
        got = next(b for b in bills if b["id"] == bill["id"])
        assert got["status"] == "stale"  # never confirmed_paid
        assert got["confirmed_at"] is None
        assert got["escalation_level"] == 3

        # The durable row agrees.
        row = await BillsRepository(registry.session_factory).get_bill(bill["id"])
        assert row is not None
        assert row.status == "stale"
        assert row.confirmed_at is None
        await engine.dispose()

    async def test_sweep_leaves_disputed_and_confirmed_alone(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        await _seed_bill(registry, payee="D", due_in_days=-2, status="disputed")
        await _seed_bill(registry, payee="P", due_in_days=-2, status="confirmed_paid")
        client = TestClient(_wired_app(registry))

        bills = client.get("/api/bills/").json()["bills"]
        by_payee = {b["payee"]: b for b in bills}
        assert by_payee["D"]["status"] == "disputed"
        assert by_payee["P"]["status"] == "confirmed_paid"
        await engine.dispose()


# ---------------------------------------------------------------------------
# 8. Escalation mirroring into durable attention
# ---------------------------------------------------------------------------


class TestEscalationAttention:
    async def test_escalated_bill_creates_one_item_and_refreshes_in_place(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="Escalating", due_in_days=3)
        client = TestClient(_wired_app(registry))

        # First read: level 1 (due within 7 days) → one durable item created.
        first = client.get("/api/bills/").json()["bills"]
        got = next(b for b in first if b["id"] == bill["id"])
        assert got["escalation_level"] == 1

        items = await _attention(registry, state="active")
        assert len(items) == 1
        item = items[0]
        assert f"bill:{bill['id']}" in [str(x) for x in (item.source_refs or [])]
        assert (item.escalation_policy or {}).get("bills.escalation_level") == 1

        # Second read: updated in place — still exactly ONE row.
        client.get("/api/bills/")
        items2 = await _attention(registry, state="active")
        assert len(items2) == 1
        assert items2[0].id == item.id
        assert items2[0].state == "active"

        # A level-0 bill (due in 30 days) must NOT create an item.
        await _seed_bill(registry, payee="Calm", due_in_days=30)
        client.get("/api/bills/")
        items3 = await _attention(registry, state="active")
        assert len(items3) == 1  # only the escalating bill's item
        await engine.dispose()

    async def test_confirm_paid_resolves_the_escalation_item(
        self, tmp_path: pathlib.Path
    ) -> None:
        engine, registry = await _make_harness(tmp_path)
        bill = await _seed_bill(registry, payee="WillPay", due_in_days=3)
        client = TestClient(_wired_app(registry))

        client.get("/api/bills/")  # creates the level-1 item
        assert client.post(f"/api/bills/{bill['id']}/confirm-paid").status_code == 200

        assert await _attention(registry, state="active") == []
        resolved = await _attention(registry, state="resolved")
        assert len(resolved) == 1
        assert f"bill:{bill['id']}" in [str(x) for x in (resolved[0].source_refs or [])]
        await engine.dispose()


# ---------------------------------------------------------------------------
# 9. House degradation (no registry)
# ---------------------------------------------------------------------------


class TestDegraded:
    def test_gets_return_200_empty_shapes(self) -> None:
        client = TestClient(_degraded_app())

        assert client.get("/api/bills/").json() == {"bills": [], "durable": False}
        assert (
            client.get("/api/bills/?status=pending").json()
            == {"bills": [], "durable": False}
        )
        assert client.get("/api/bills/upcoming?days=14").json() == {
            "bills": [],
            "days": 14,
            "durable": False,
        }
        cal = client.get("/api/bills/calendar?month=2026-11").json()
        assert cal["month"] == "2026-11"
        assert cal["days"] == []
        assert cal["durable"] is False
        for path in (
            "/api/bills/",
            "/api/bills/?status=stale",
            "/api/bills/upcoming",
            "/api/bills/calendar?month=2026-11",
        ):
            assert client.get(path).status_code == 200

    def test_writes_return_202_executed_false(self) -> None:
        client = TestClient(_degraded_app())

        r = client.post(
            "/api/bills/",
            json={
                "payee": "Acme",
                "amount_cents": 100,
                "due_at": "2027-01-01T00:00:00Z",
            },
        )
        assert r.status_code == 202
        assert r.json() == {"executed": False, "reason": "persistence_not_configured"}

        r2 = client.post("/api/bills/whatever/confirm-paid")
        assert r2.status_code == 202
        assert r2.json() == {"executed": False, "reason": "persistence_not_configured"}

        r3 = client.patch("/api/bills/whatever", json={"payee": "X"})
        assert r3.status_code == 202
        assert r3.json() == {"executed": False, "reason": "persistence_not_configured"}


# ---------------------------------------------------------------------------
# Determinism sanity: the escalation ladder itself
# ---------------------------------------------------------------------------


class TestEscalationLadder:
    def test_levels_by_due_distance(self) -> None:
        from backend.bills.service import bill_escalation_level

        now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

        def mk(due_in_days: float, status: str = "pending") -> dict[str, Any]:
            return {
                "id": "b1",
                "status": status,
                "confirmed_at": None,
                "due_at": now + timedelta(days=due_in_days),
            }

        assert bill_escalation_level(mk(10), now=now) == 0  # > 7 days
        assert bill_escalation_level(mk(3), now=now) == 1  # within 7 days
        assert bill_escalation_level(mk(1), now=now) == 2  # within 48 hours
        assert bill_escalation_level(mk(-1), now=now) == 3  # overdue
        assert bill_escalation_level(mk(3, "confirmed_paid"), now=now) is None
        assert bill_escalation_level(mk(-1, "disputed"), now=now) == 1
        assert bill_escalation_level(mk(-1, "stale"), now=now) == 3
        assert bill_escalation_level(
            {"id": "b2", "status": "pending", "confirmed_at": None, "due_at": None},
            now=now,
        ) is None
